import io
from types import SimpleNamespace

import httpx

from app import repo, worker
from app.db import connection
from app.ingest import transcribe as tr

ARTICLE = """<html><head><title>Lech-Artikel</title>
<meta name="author" content="Erika Muster">
<meta property="article:published_time" content="2023-06-01T08:00:00Z">
<meta property="og:site_name" content="Augsburger Beispielblatt"></head>
<body><nav>Menü Startseite</nav><article><h1>Streit um Wasserrechte am Lech</h1>
<p>Die Genehmigung für die Wasserentnahme wurde bereits im Jahr 2019 erteilt, wie aus Unterlagen hervorgeht, die dieser Redaktion vorliegen.</p>
<p>Anwohner kritisieren das Verfahren seit Monaten und fordern eine erneute Prüfung durch die zuständige Behörde.</p>
</article><footer>Impressum</footer></body></html>"""


def _mk_project(client, name="Lech"):
    r = client.post("/projects", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_home_and_project_pages(client):
    assert client.get("/").status_code == 200
    pid = _mk_project(client)
    r = client.get(f"/p/{pid}")
    assert r.status_code == 200 and "Lech" in r.text
    assert client.get(f"/p/{pid}/timeline").status_code == 200
    assert client.get(f"/p/{pid}/highlights").status_code == 200
    assert client.get("/p/999").status_code == 404


def test_web_ingest_flow(client, monkeypatch):
    pid = _mk_project(client)

    def fake_get(url, **kw):
        return httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html; charset=utf-8"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("app.ingest.web.httpx.get", fake_get)
    r = client.post(f"/p/{pid}/sources/url", data={"url": "example.org/lech", "tags": "Presse"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert worker.drain() == 1
    with connection() as conn:
        s = conn.execute("SELECT * FROM sources").fetchone()
    assert s["state"] == "bereit", s["error"]
    assert s["url"] == "https://example.org/lech"
    assert s["title"] == "Streit um Wasserrechte am Lech"
    assert s["author"] == "Erika Muster"
    assert s["published_at"] == "2023-06-01"
    assert "Genehmigung" in s["text"] and "Impressum" not in s["text"]
    assert len(s["content_hash"]) == 64
    page = client.get(f"/s/{s['id']}")
    assert page.status_code == 200 and "Genehmigung" in page.text
    snap = client.get(f"/s/{s['id']}/snapshot")
    assert snap.status_code == 200 and "sandbox" in snap.headers["content-security-policy"]


def test_media_url_routes_to_media_job(client, monkeypatch):
    pid = _mk_project(client)
    client.post(f"/p/{pid}/sources/url", data={"url": "https://www.youtube.com/watch?v=abc"})
    with connection() as conn:
        job = conn.execute("SELECT type FROM jobs").fetchone()
        src = conn.execute("SELECT kind, state FROM sources").fetchone()
    assert job["type"] == "media" and src["kind"] == "video" and src["state"] == "wartet"


def test_note_highlight_api_and_export(client):
    pid = _mk_project(client)
    r = client.post(f"/p/{pid}/sources/note", data={
        "title": "Telefonat Pressestelle", "text": "Sprecher bestätigt: Bescheid vom 12.03.2019 existiert.",
        "event_date": "12.03.2019", "tags": "Telefonat"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[-1])
    text = client.get(f"/s/{sid}").text
    assert "Bescheid" in text

    with connection() as conn:
        full = repo.get_source(conn, sid)["text"]
    start = full.index("Bescheid")
    r = client.post("/api/highlights", json={"source_id": sid, "start": start, "end": start + 8, "color": "rot"})
    hid = r.json()["id"]
    assert client.post("/api/highlights", json={"source_id": sid, "start": 5, "end": 999}).status_code == 400
    assert client.patch(f"/api/highlights/{hid}", json={"note": "Bescheid anfordern", "tags": "To-do",
                                                         "event_date": "2019"}).json()["ok"]
    page = client.get(f"/s/{sid}").text
    assert f'data-ids="{hid}"' in page and "Bescheid anfordern" in page

    s = client.get(f"/search?q=anfordern&project={pid}")
    assert f"/s/{sid}?h={hid}#hl-{hid}" in s.text

    md = client.get(f"/p/{pid}/export.md").text
    assert "# Lech" in md and "> Bescheid" in md and "Bescheid anfordern" in md

    tl = client.get(f"/p/{pid}/timeline").text
    assert "Telefonat Pressestelle" in tl and "12. März 2019" in tl

    assert client.delete(f"/api/highlights/{hid}").json()["ok"]
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0


def test_upload_text_and_audio_transcription(client, monkeypatch):
    pid = _mk_project(client)
    r = client.post(f"/p/{pid}/sources/upload",
                    files={"files": ("protokoll.txt", io.BytesIO("Zeile eins\nZeile zwei".encode()), "text/plain")},
                    follow_redirects=False)
    assert r.status_code == 303
    worker.drain()
    with connection() as conn:
        s = conn.execute("SELECT * FROM sources WHERE title = 'protokoll'").fetchone()
    assert s["kind"] == "document" and s["text"] == "Zeile eins\nZeile zwei" and s["state"] == "bereit"

    # Audio: Transkription mit einem Ersatzmodell
    class FakeModel:
        def transcribe(self, path, **kw):
            segs = [SimpleNamespace(start=0.0, end=2.0, text=" Guten Tag."),
                    SimpleNamespace(start=2.0, end=5.0, text=" Es geht um den Lech.")]
            return iter(segs), SimpleNamespace(language="de", duration=5.0)

    monkeypatch.setattr(tr, "_get_model", lambda: FakeModel())
    client.post(f"/p/{pid}/sources/upload",
                files={"files": ("interview.mp3", io.BytesIO(b"ID3fake"), "audio/mpeg")})
    assert worker.drain() == 2  # file → transcribe
    with connection() as conn:
        a = conn.execute("SELECT * FROM sources WHERE title = 'interview'").fetchone()
        assert a["state"] == "bereit", a["error"]
        assert a["text"] == "Guten Tag.\nEs geht um den Lech."
        assert a["media_path"] and a["language"] == "de"
    page = client.get(f"/s/{a['id']}").text
    assert '<audio id="player"' in page and 'data-t="2.0"' in page
    res = client.get(f"/search?q=lech&project={pid}").text
    assert "?t=2.0" in res


def test_failed_job_is_reported(client, monkeypatch):
    pid = _mk_project(client)

    def boom(url, **kw):
        raise httpx.ConnectError("keine Verbindung")

    monkeypatch.setattr("app.ingest.web.httpx.get", boom)
    client.post(f"/p/{pid}/sources/url", data={"url": "https://example.org/x", "mode": "web"})
    worker.drain()
    with connection() as conn:
        s = conn.execute("SELECT * FROM sources").fetchone()
    assert s["state"] == "fehler" and "keine Verbindung" in s["error"]
    assert "Erneut versuchen" in client.get(f"/s/{s['id']}").text


def test_file_path_traversal_blocked(client):
    assert client.get("/files/../fundus.db").status_code == 404
    assert client.get("/files/%2e%2e/fundus.db").status_code == 404


def test_basic_auth(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "AUTH_PASSWORD", "geheim")
    assert client.get("/").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/", auth=("fundus", "geheim")).status_code == 200


def test_delete_project_requires_name(client):
    pid = _mk_project(client, "Weg damit")
    client.post(f"/p/{pid}/delete", data={"confirm": "falsch"})
    assert client.get(f"/p/{pid}").status_code == 200
    client.post(f"/p/{pid}/delete", data={"confirm": "Weg damit"})
    assert client.get(f"/p/{pid}").status_code == 404
