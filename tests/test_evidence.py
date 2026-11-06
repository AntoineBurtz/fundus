import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app import config, evidence, repo, tsa, worker
from app.db import connection
from app.diff import diff_blocks
from app.ingest import watch

PAGE = """<html><head><meta charset="utf-8"><title>Bescheid</title></head><body><article>
<h1>Wasserentnahme am Lech</h1>
<p>Die Genehmigung wurde im März 2019 erteilt, wie aus Unterlagen hervorgeht, die der Redaktion vorliegen.</p>
<p>Das Unternehmen darf pro Jahr rund {menge} Millionen Kubikmeter Wasser fördern, so das Landratsamt.</p>
<p>Bürgerinitiativen fordern eine erneute Prüfung und Einsicht in die Messdaten der Behörde.</p>
</article></body></html>"""

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl fehlt")


# ------------------------------------------------------------- Test-Zeitstempeldienst (openssl)

@pytest.fixture()
def local_tsa(tmp_path_factory, monkeypatch):
    """Ein echter RFC-3161-Dienst auf Basis von `openssl ts`, angebunden über httpx.post."""
    d = tmp_path_factory.mktemp("tsa")
    (d / "tsa.cnf").write_text("""[ req ]
distinguished_name = dn
prompt = no
[ dn ]
CN = Fundus Test TSA
[ ext ]
extendedKeyUsage = critical,timeStamping
basicConstraints = CA:FALSE
[ tsa ]
default_tsa = t
[ t ]
dir = .
serial = ./serial
crypto_device = builtin
signer_cert = ./tsa.crt
signer_key = ./tsa.key
signer_digest = sha256
default_policy = 1.2.3.4.1
digests = sha256
accuracy = secs:1
ess_cert_id_alg = sha256
""")
    (d / "serial").write_text("01\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "tsa.key",
                    "-out", "tsa.crt", "-days", "30", "-config", "tsa.cnf", "-extensions", "ext"],
                   cwd=d, check=True, capture_output=True)

    def fake_post(url, content=None, **kw):
        if "kaputt" in url:
            raise httpx.ConnectError("nicht erreichbar")
        (d / "q.tsq").write_bytes(content)
        subprocess.run(["openssl", "ts", "-reply", "-config", "tsa.cnf", "-queryfile", "q.tsq",
                        "-out", "r.tsr"], cwd=d, check=True, capture_output=True)
        return httpx.Response(200, content=(d / "r.tsr").read_bytes(), request=httpx.Request("POST", url))

    monkeypatch.setattr("app.tsa.httpx.post", fake_post)
    monkeypatch.setattr(config, "TSA_URLS", ["http://kaputt.example/tsr", "http://tsa.test/"])
    return d


@needs_openssl
def test_tsa_roundtrip_and_checks(local_tsa):
    digest = hashlib.sha256(b"inhalt").digest()
    ts = tsa.stamp(digest, config.TSA_URLS)
    assert ts.url == "http://tsa.test/" and ts.gen_time.tzinfo is not None
    with pytest.raises(tsa.TimestampError, match="andere Prüfsumme"):
        tsa.parse_response(ts.token, hashlib.sha256(b"anders").digest(), None)
    with pytest.raises(tsa.TimestampError):
        tsa.parse_response(b"kein asn1", digest, None)
    with pytest.raises(tsa.TimestampError, match="kaputt"):
        tsa.stamp(digest, ["http://kaputt.example/"])


# ------------------------------------------------------------- Webseite: Fassung 1, Manifest, Zeitstempel, Nachweis

class Site:
    def __init__(self):
        self.status = 200
        self.menge = "1,2"
        self.body = None

    def get(self, url, **kw):
        body = self.body if self.body is not None else PAGE.format(menge=self.menge)
        return httpx.Response(self.status, text=body, headers={"content-type": "text/html; charset=utf-8"},
                              request=httpx.Request("GET", url))


@pytest.fixture()
def site(monkeypatch):
    s = Site()
    monkeypatch.setattr("app.ingest.web.httpx.get", s.get)
    monkeypatch.setattr("app.ingest.watch.httpx.get", s.get)
    return s


def _add_web(client, url="https://amt.example/bescheid"):
    r = client.post("/projects", data={"name": "P"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/p/{pid}/sources/url", data={"url": url, "mode": "web"})
    worker.drain()
    with connection() as conn:
        return conn.execute("SELECT id FROM sources ORDER BY id DESC").fetchone()["id"]


@needs_openssl
def test_web_capture_is_sealed_and_exportable(client, site, local_tsa):
    sid = _add_web(client)
    with connection() as conn:
        v = evidence.list_versions(conn, sid)[0]
        arts = evidence.artifacts_for(conn, v["id"])
    assert v["tsa_url"] == "http://tsa.test/" and v["tsa_time"] and v["tsa_error"] is None
    manifest = json.loads((config.FILES_DIR / v["manifest_path"]).read_bytes())
    assert manifest["fassung"] == 1 and manifest["dateien"][0]["sha256"] == arts[0]["sha256"]
    assert hashlib.sha256((config.FILES_DIR / v["manifest_path"]).read_bytes()).hexdigest() == v["manifest_sha256"]

    r = client.get(f"/s/{sid}/v/1/nachweis.zip")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"manifest.json", "manifest.tsr", "sha256sums.txt", "PRUEFEN.txt", "dateien/page.html"} <= names
    assert "openssl ts -verify" in z.read("PRUEFEN.txt").decode()
    # Unabhängige Prüfung mit openssl wie in der Anleitung beschrieben
    out = local_tsa / "check"
    out.mkdir()
    z.extractall(out)
    res = subprocess.run(["openssl", "ts", "-verify", "-in", "manifest.tsr", "-data", "manifest.json",
                          "-CAfile", str(local_tsa / "tsa.crt")], cwd=out, capture_output=True, text=True)
    assert "Verification: OK" in res.stdout, res.stderr

    page = client.get(f"/s/{sid}").text
    assert "Fassung 1" in page and "✓ Zeitstempel" in page and "Nachweis herunterladen" in page


def test_missing_timestamp_can_be_requested_again(client, site, monkeypatch):
    monkeypatch.setattr(config, "TSA_URLS", ["http://kaputt.example/"])
    monkeypatch.setattr("app.tsa.httpx.post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    sid = _add_web(client)
    with connection() as conn:
        v = evidence.list_versions(conn, sid)[0]
        assert v["tsa_time"] is None and "offline" in v["tsa_error"] and v["manifest_sha256"]
        assert repo.get_source(conn, sid)["state"] == "bereit"  # Sicherung selbst ist gültig
    assert "erneut anfordern" in client.get(f"/s/{sid}/v/1").text
    client.post(f"/s/{sid}/v/1/seal")
    with connection() as conn:
        assert conn.execute("SELECT type FROM jobs ORDER BY id DESC").fetchone()["type"] == "seal"


# ------------------------------------------------------------- Überwachung

def test_recheck_detects_change_delete_and_noise(client, site, monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "notify", lambda src, msg: sent.append(msg))
    sid = _add_web(client)

    def recheck(force=False):
        with connection() as conn:
            repo.enqueue_job(conn, sid, "recheck", {"force": force})
        worker.drain()
        with connection() as conn:
            return repo.get_source(conn, sid), evidence.list_versions(conn, sid)

    s, versions = recheck()
    assert len(versions) == 1 and s["watch_status"] == "unverändert" and not s["watch_alert"]

    site.menge = "1,6"
    s, versions = recheck()
    assert [v["n"] for v in versions] == [2, 1] and versions[0]["reason"] == "änderung"
    assert s["watch_alert"] == 1 and s["watch_status"] == "geändert" and sent == ["Inhalt hat sich geändert (Fassung 2)"]
    assert "1,2" in s["text"]  # Lesetext (und damit Markierungen) bleibt bei Fassung 1

    page = client.get(f"/s/{sid}/v/2").text
    assert "<del>1,2</del><ins>1,6</ins>" in page and "1 geändert" in page
    with connection() as conn:
        assert repo.get_source(conn, sid)["watch_alert"] == 0  # angesehen

    site.body = "<html><body></body></html>"  # Bot-Sperre o. Ä.
    s, versions = recheck()
    assert len(versions) == 2 and s["watch_status"].startswith("kein Text")

    site.body, site.status = None, 404
    s, versions = recheck()
    s, versions = recheck()  # zweites 404 erzeugt keine weitere Fassung
    assert [v["reason"] for v in versions][:2] == ["gelöscht", "änderung"] and len(versions) == 3
    assert s["watch_status"] == "gelöscht" and len(sent) == 2

    site.status = 200
    s, versions = recheck(force=True)
    assert versions[0]["reason"] == "manuell" and versions[0]["n"] == 4


def test_watch_schedule(client, site):
    sid = _add_web(client)
    client.post(f"/s/{sid}/watch", data={"hours": 24})
    with connection() as conn:
        s = repo.get_source(conn, sid)
        assert s["watch_hours"] == 24 and s["next_check_at"] > s["captured_at"]
        assert watch.enqueue_due_checks(conn) == 0
        conn.execute("UPDATE sources SET next_check_at = '2000-01-01T00:00:00Z' WHERE id = ?", (sid,))
        assert watch.enqueue_due_checks(conn) == 1
        assert watch.enqueue_due_checks(conn) == 0  # nicht doppelt
    assert "überwacht" in client.get("/p/1").text
    client.post(f"/s/{sid}/watch", data={"hours": 0})
    with connection() as conn:
        assert repo.get_source(conn, sid)["watch_hours"] is None


def test_notify_formats(monkeypatch):
    calls = []
    monkeypatch.setattr("app.ingest.watch.httpx.post", lambda url, **kw: calls.append((url, kw)))
    monkeypatch.setattr(config, "BASE_URL", "http://fundus.lan")
    src = {"id": 7, "title": "Bescheid", "url": "https://x"}
    monkeypatch.setattr(config, "NOTIFY_URL", "https://ntfy.sh/meine-recherche")
    watch.notify(src, "geändert")
    monkeypatch.setattr(config, "NOTIFY_URL", "http://ha.lan/api/webhook/fundus")
    watch.notify(src, "geändert")
    assert calls[0][1]["headers"]["Click"] == "http://fundus.lan/s/7" and calls[0][1]["content"] == "geändert".encode()
    assert calls[1][1]["json"]["url"] == "http://fundus.lan/s/7"


def test_archive_org(client, site, monkeypatch):
    sid = _add_web(client)

    def fake_get(url, **kw):
        assert url.startswith("https://web.archive.org/save/https://amt.example/bescheid")
        return httpx.Response(302, headers={"location": "/web/20261106120000/https://amt.example/bescheid"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("app.ingest.watch.httpx.get", fake_get)
    client.post(f"/s/{sid}/run/archive")
    worker.drain()
    with connection() as conn:
        assert repo.get_source(conn, sid)["archive_url"] == \
            "https://web.archive.org/web/20261106120000/https://amt.example/bescheid"
    assert "archive.org ↗" in client.get(f"/s/{sid}").text


def test_artifact_headers(client, site):
    sid = _add_web(client)
    with connection() as conn:
        a = conn.execute("SELECT id FROM artifacts WHERE source_id = ?", (sid,)).fetchone()
        vid = evidence.list_versions(conn, sid)[0]["id"]
        pdf = config.FILES_DIR / str(sid) / "v1" / "seite.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        pid = evidence.add_artifact(conn, sid, vid, "pdf", pdf, "application/pdf")
    html = client.get(f"/a/{a['id']}")
    assert "sandbox" in html.headers["content-security-policy"]
    r = client.get(f"/a/{pid}")
    assert r.status_code == 200 and "content-security-policy" not in r.headers
    assert client.get("/a/9999").status_code == 404


# ------------------------------------------------------------- Diff

def test_diff_blocks():
    old = "A eins\nB zwei\nC drei\nD vier\nE fünf\nF sechs"
    new = "A eins\nB zwei neu\nC drei\nD vier\nE fünf\nF sechs\nG sieben"
    blocks, stats = diff_blocks(old, new)
    assert stats == {"added": 1, "removed": 0, "changed": 1}
    types = [b["type"] for b in blocks]
    assert types == ["same", "change", "same", "skip", "same", "ins"]
    assert blocks[1]["html"] == "B zwei<ins> neu</ins>"
    assert diff_blocks("<b>x</b>", "<b>y</b>")[0][0]["html"] == "<del>&lt;b&gt;x&lt;/b&gt;</del><ins>&lt;b&gt;y&lt;/b&gt;</ins>"


# ------------------------------------------------------------- Echter Browser (optional)

CHROMIUM = os.environ.get("FUNDUS_TEST_CHROMIUM")


@pytest.mark.skipif(not CHROMIUM, reason="FUNDUS_TEST_CHROMIUM nicht gesetzt")
def test_capture_with_real_browser(client, tmp_path, monkeypatch):
    (tmp_path / "seite.html").write_text(
        '<html><head><meta charset="utf-8"><title>JS</title></head><body><div id="r">Lädt…</div>'
        '<div id="c" style="position:fixed;bottom:0">Cookies <button onclick="this.parentNode.remove()">'
        'Alle akzeptieren</button></div><script>setTimeout(()=>{document.getElementById("r").innerHTML='
        '"<article><h1>Bescheid</h1>"+Array.from({length:5},(_,i)=>"<p>Absatz "+i+": Das Landratsamt hat '
        'den Bescheid zur Wasserentnahme veröffentlicht, samt Gutachten und Messdaten der Fachbehörden.</p>")'
        '.join("")+"</article>"},200)</script></body></html>', encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "CAPTURE", True)
    monkeypatch.setattr(config, "CHROMIUM_PATH", CHROMIUM)
    try:
        sid = _add_web(client, f"http://127.0.0.1:{server.server_port}/seite.html")
    finally:
        server.shutdown()
    with connection() as conn:
        s = repo.get_source(conn, sid)
        v = evidence.list_versions(conn, sid)[0]
        kinds = {a["kind"] for a in evidence.artifacts_for(conn, v["id"])}
    assert {"html", "screenshot", "pdf", "rendered"} <= kinds
    assert "Cookie-Hinweis automatisch bestätigt" in v["note"]
    assert "JavaScript" in v["note"] and "Absatz 4" in s["text"]
    assert (config.FILES_DIR / str(sid) / "v1" / "screenshot.png").read_bytes()[:4] == b"\x89PNG"
