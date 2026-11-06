import json
import subprocess

import pytest

from app import config, evidence, repo, social, worker
from app.db import connection
from app.ingest import accounts

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tcsrftoken\txyz\n"
    "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc123\n"
)


def test_normalize_handle_and_urls():
    assert social.normalize_handle("instagram", "@Landratsamt.AUX") == "landratsamt.aux"
    assert social.normalize_handle("instagram", "https://www.instagram.com/stadt_augsburg/?hl=de") == "stadt_augsburg"
    assert social.normalize_handle("tiktok", "https://www.tiktok.com/@br24/video/1") == "br24"
    with pytest.raises(ValueError):
        social.normalize_handle("instagram", "kein name mit leerzeichen")
    acc = {"platform": "instagram", "handle": "x", "stories": 1, "highlights": 1, "posts": 0}
    assert [k for k, _ in social.content_urls(acc)] == ["stories", "highlights"]
    assert social.content_urls({"platform": "tiktok", "handle": "x", "stories": 1, "highlights": 0,
                                "posts": 1})[0][1] == "https://www.tiktok.com/@x/stories"


def test_cookies(app_env):
    info = social.save_cookies("instagram", COOKIES.encode())
    assert info["session"] and info["expires"].startswith("2030")
    st = social.cookie_status("instagram")
    assert st["present"] and st["session"]
    assert oct(social.cookie_path("instagram").stat().st_mode)[-3:] == "600"
    with pytest.raises(ValueError, match="Netscape"):
        social.save_cookies("instagram", b"{\"json\": true}")
    with pytest.raises(ValueError, match="tiktok.com"):
        social.save_cookies("tiktok", COOKIES.encode())
    social.delete_cookies("instagram")
    assert not social.cookie_status("instagram")["present"]


def test_settings_page_upload(client):
    assert client.get("/einstellungen").status_code == 200
    r = client.post("/einstellungen/cookies/instagram",
                    files={"file": ("cookies.txt", COOKIES.encode(), "text/plain")}, follow_redirects=True)
    assert "Cookies für Instagram gespeichert" in r.text and "✓ Anmelde-Cookie vorhanden" in r.text


def _project(client):
    r = client.post("/projects", data={"name": "Lech"}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_add_account_and_schedule(client):
    pid = _project(client)
    r = client.post(f"/p/{pid}/accounts", data={"platform": "instagram", "handle": "@Stadt_Augsburg",
                                                "stories": "1", "interval_hours": "3"}, follow_redirects=True)
    assert "@stadt_augsburg" in r.text and "In der Warteschlange" in r.text
    dup = client.post(f"/p/{pid}/accounts", data={"platform": "instagram", "handle": "stadt_augsburg",
                                                  "stories": "1"}, follow_redirects=True)
    assert "bereits beobachtet" in dup.text
    none = client.post(f"/p/{pid}/accounts", data={"platform": "tiktok", "handle": "br24"}, follow_redirects=True)
    assert "mindestens eine Inhaltsart" in none.text
    with connection() as conn:
        acc = conn.execute("SELECT * FROM watched_accounts").fetchone()
        assert social.enqueue_check(conn, acc["id"]) is None  # schon eingereiht
        conn.execute("UPDATE jobs SET status = 'done'")
        conn.execute("UPDATE watched_accounts SET next_check_at = '2000-01-01T00:00:00Z'")
        assert social.enqueue_due_accounts(conn) == 1
        nxt = conn.execute("SELECT next_check_at FROM watched_accounts").fetchone()[0]
        assert nxt > "2026"  # neu geplant (mit Zufallsanteil)
        assert repo.claim_next_job(conn, "schnell") is None
        assert repo.claim_next_job(conn, "social")["type"] == "account"


class FakeGalleryDL:
    """Legt Dateien so ab, wie gallery-dl mit --write-metadata es tut."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def __call__(self, url, dest, cookies, archive, date_min=None):
        self.calls.append((url, date_min))
        if self.fail:
            return subprocess.CompletedProcess([], 1, "", self.fail)
        if "/stories/" in url:
            (dest / "story1.mp4").write_bytes(b"video")
            (dest / "story1.mp4.json").write_text(json.dumps({
                "media_id": "3412", "num": 1, "date": "2026-11-06 19:30:00", "username": "stadt_augsburg",
                "subcategory": "stories"}))
            (dest / "story2.jpg").write_bytes(b"\xff\xd8bild")
            (dest / "story2.jpg.json").write_text(json.dumps({
                "media_id": "3413", "num": 1, "date": "2026-11-06 20:00:00", "description": "Pegel heute"}))
        return subprocess.CompletedProcess([], 0, "", "")


@pytest.fixture()
def fake_gdl(monkeypatch, app_env):
    fake = FakeGalleryDL()
    monkeypatch.setattr(accounts, "run_gallery_dl", fake)
    social.save_cookies("instagram", COOKIES.encode())
    return fake


def test_check_imports_stories_once(client, fake_gdl):
    pid = _project(client)
    client.post(f"/p/{pid}/accounts", data={"platform": "instagram", "handle": "stadt_augsburg",
                                            "stories": "1", "posts": "1", "transcribe": "1"})
    worker.drain()
    with connection() as conn:
        srcs = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        acc = conn.execute("SELECT * FROM watched_accounts").fetchone()
        jobs = [r["type"] for r in conn.execute("SELECT type FROM jobs ORDER BY id")]
        video = srcs[0]
        arts = [a["kind"] for a in evidence.artifacts_for(conn, evidence.list_versions(conn, video["id"])[0]["id"])]
        tags = repo.source_tags(conn, [video["id"]])[video["id"]]
    assert [s["kind"] for s in srcs] == ["video", "image"]
    assert video["title"].startswith("Instagram-Story von @stadt_augsburg, 06.11.2026")
    assert video["url"] == "https://www.instagram.com/stories/stadt_augsburg/3412/"
    assert video["published_at"] == "2026-11-06" and video["external_id"] == "instagram:3412:1"
    assert video["media_path"].endswith("v1/stories.mp4") and srcs[1]["media_path"] is None
    assert srcs[1]["description"] == "Pegel heute"
    assert arts == ["media", "metadata"] and set(tags) == {"Story", "@stadt_augsburg"}
    assert jobs.count("seal") == 2 and jobs.count("transcribe") == 1
    assert acc["last_status"] == "2 neu" and acc["item_count"] == 2
    # Beiträge nur ab kurz vor Beobachtungsbeginn
    posts_call = [c for c in fake_gdl.calls if c[0].endswith("/posts/")][0]
    assert posts_call[1] is not None

    # Zweiter Abruf liefert dieselben Dateien → keine Duplikate
    with connection() as conn:
        social.enqueue_check(conn, acc["id"])
    worker.drain()
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
        assert conn.execute("SELECT last_status FROM watched_accounts").fetchone()[0] == "nichts Neues"

    page = client.get(f"/p/{pid}/accounts").text
    assert "2 gesichert" in page and "nichts Neues" in page
    assert client.get(f"/s/{srcs[1]['id']}").status_code == 200


def test_expired_login_is_reported(client, fake_gdl, monkeypatch):
    sent = []
    monkeypatch.setattr("app.ingest.watch.notify", lambda src, msg: sent.append(msg))
    fake_gdl.fail = "[instagram][error] HttpError: '401 Unauthorized' – Redirected to login page"
    pid = _project(client)
    client.post(f"/p/{pid}/accounts", data={"platform": "instagram", "handle": "x", "stories": "1"})
    worker.drain()
    with connection() as conn:
        acc = conn.execute("SELECT * FROM watched_accounts").fetchone()
        social.enqueue_check(conn, acc["id"])
    worker.drain()
    assert acc["last_status"] == "Anmeldung abgelaufen oder blockiert" and acc["last_error"]
    assert sent == ["Anmeldung abgelaufen – bitte neue Cookies hochladen"]  # nur einmal


def test_no_cookies(client, monkeypatch):
    monkeypatch.setattr(accounts, "run_gallery_dl", lambda *a, **k: pytest.fail("darf nicht laufen"))
    pid = _project(client)
    client.post(f"/p/{pid}/accounts", data={"platform": "tiktok", "handle": "br24", "stories": "1"})
    worker.drain()
    page = client.get(f"/p/{pid}/accounts").text
    assert "Keine Cookies hinterlegt" in page and "Einrichten" in page


def test_classify():
    cp = lambda rc, err: subprocess.CompletedProcess([], rc, "", err)
    assert accounts.classify(cp(0, "")) is None
    assert accounts.classify(cp(1, "HTTP Error 429 Too Many Requests")) == "rate"
    assert accounts.classify(cp(1, "Redirected to login page")) == "auth"
    assert accounts.classify(cp(4, "User not found")) == "notfound"
    assert accounts.classify(cp(4, "irgendwas")) == "error"


def test_delete_account_keeps_sources(client, fake_gdl):
    pid = _project(client)
    client.post(f"/p/{pid}/accounts", data={"platform": "instagram", "handle": "x", "stories": "1"})
    worker.drain()
    with connection() as conn:
        aid = conn.execute("SELECT id FROM watched_accounts").fetchone()[0]
    client.post(f"/accounts/{aid}/delete")
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watched_accounts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE account_id IS NULL").fetchone()[0] == 2
