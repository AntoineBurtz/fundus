import io
import sqlite3
from types import SimpleNamespace

import pytest

from app import config, db, repo, worker
from app.db import connection
from app.ingest import transcribe as tr
from app.ingest.subtitles import merge_cues, parse_subtitles, parse_vtt_srt, pick_subtitles

# Typische YouTube-Auto-Untertitel: jede Zeile erscheint zweimal ("Roll-up"),
# dazwischen 10-ms-Übergangs-Cues und Wort-Zeitstempel.
YOUTUBE_VTT = """WEBVTT
Kind: captions
Language: de

00:00:00.000 --> 00:00:02.500 align:start position:0%
Herzlich<00:00:00.400><c> willkommen</c><00:00:00.900><c> zum</c><00:00:01.100><c> Podcast</c>

00:00:02.500 --> 00:00:02.510 align:start position:0%
Herzlich willkommen zum Podcast


00:00:02.510 --> 00:00:05.000 align:start position:0%
Herzlich willkommen zum Podcast
heute<00:00:03.000><c> geht</c><00:00:03.400><c> es</c><00:00:03.600><c> um</c><00:00:03.800><c> den</c><00:00:04.000><c> Lech.</c>

00:00:05.000 --> 00:00:05.010 align:start position:0%
heute geht es um den Lech.


00:00:05.010 --> 00:00:08.000 align:start position:0%
heute geht es um den Lech.
Wir sprechen mit Anwohnern &amp; Behörden.
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Erste Zeile

2
00:00:03,000 --> 00:00:06,500
<i>Zweite</i> Zeile.
"""

TTML = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" ttp:tickRate="10000000">
 <body><div>
  <p begin="00:00:01.000" end="00:00:03.000">Guten Abend<br/>meine Damen und Herren.</p>
  <tt:p begin="40000000t" end="60000000t">Das Wasser wird knapp.</tt:p>
 </div></body></tt>"""


def test_youtube_rollup_vtt_is_deduplicated():
    cues = parse_vtt_srt(YOUTUBE_VTT)
    text = " ".join(c["text"] for c in cues)
    assert text == ("Herzlich willkommen zum Podcast heute geht es um den Lech. "
                    "Wir sprechen mit Anwohnern & Behörden.")
    assert cues[0]["start"] == 0.0


def test_merge_into_sentences():
    # Satzende nach 58 Zeichen ist unter der Mindestlänge → wird mit dem nächsten Satz verbunden
    segs = parse_subtitles(YOUTUBE_VTT, "vtt")
    assert len(segs) == 1 and segs[0]["start"] == 0.0 and segs[0]["end"] == 8.0
    segs = merge_cues(parse_vtt_srt(YOUTUBE_VTT), min_chars=20)
    assert [s["text"] for s in segs] == ["Herzlich willkommen zum Podcast heute geht es um den Lech.",
                                         "Wir sprechen mit Anwohnern & Behörden."]
    assert segs[0]["end"] == 5.0
    long = merge_cues([{"start": i, "end": i + 1, "text": "wort " * 10} for i in range(30)])
    assert all(len(s["text"]) <= 300 and s["end"] - s["start"] <= 21 for s in long)


def test_srt_and_ttml():
    assert [c["text"] for c in parse_vtt_srt(SRT)] == ["Erste Zeile", "Zweite Zeile."]
    segs = parse_subtitles(TTML, "ttml")
    assert segs[0]["text"] == "Guten Abend meine Damen und Herren. Das Wasser wird knapp."
    assert segs[0]["start"] == 1.0 and segs[0]["end"] == 6.0


def _fmt(ext="vtt"):
    return [{"ext": "json3", "url": "u-json"}, {"ext": ext, "url": f"u-{ext}"}]


def test_pick_subtitles_preferences():
    info = {"language": "de",
            "subtitles": {"en": _fmt(), "de-DE": _fmt(), "live_chat": _fmt()},
            "automatic_captions": {"de-orig": _fmt()}}
    assert pick_subtitles(info, "auto", ["de", "en"])["lang"] == "de-DE"
    assert pick_subtitles(info, "auto", ["en"])["lang"] == "en"
    assert pick_subtitles(info, "off") is None

    # Nur automatische: Originalspur ja, automatische Übersetzungen nein
    auto_only = {"language": "de", "subtitles": {},
                 "automatic_captions": {"fr": _fmt(), "en": _fmt(), "de-orig": _fmt(), "de": _fmt()}}
    pick = pick_subtitles(auto_only, "auto", ["en"])
    assert pick["lang"] == "de" and pick["auto"] is True and pick["url"] == "u-vtt"
    assert pick_subtitles(auto_only, "manual", ["en"]) is None
    assert pick_subtitles({"language": None, "automatic_captions": {"en": _fmt()}}, "auto") is None
    # ARD/ZDF: einzige handgemachte Spur in TTML
    assert pick_subtitles({"subtitles": {"deu": [{"ext": "ttml", "url": "x"}]}}, "auto", ["de"])["ext"] == "ttml"


# ------------------------------------------------------------- Medien-Pipeline mit Ersatz-yt-dlp

class FakeYDL:
    info: dict = {}
    subtitle_text = YOUTUBE_VTT

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        assert download is False
        return dict(self.info)

    def urlopen(self, url):
        return io.BytesIO(self.subtitle_text.encode())

    def process_ie_result(self, info, download=True):
        path = self.opts["outtmpl"].replace("%(ext)s", "m4a")
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        open(path, "wb").write(b"audio")
        return info


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.info = {"title": "Lech-Podcast", "uploader": "BR", "upload_date": "20240702",
                    "duration": 8, "language": "de", "vcodec": "none", "extractor_key": "Youtube",
                    "subtitles": {}, "automatic_captions": {"de-orig": [{"ext": "vtt", "url": "s"}]}}
    return FakeYDL


def _project(client):
    r = client.post("/projects", data={"name": "P"}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_media_uses_subtitles_and_skips_whisper(client, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(tr, "_get_model", lambda name: pytest.fail("Whisper darf nicht laufen"))
    pid = _project(client)
    client.post(f"/p/{pid}/sources/url", data={"url": "https://youtube.com/watch?v=1"})
    assert worker.drain() == 1
    with connection() as conn:
        s = conn.execute("SELECT * FROM sources").fetchone()
        assert s["state"] == "bereit", s["error"]
        assert s["transcript_origin"] == "automatische Untertitel (de)"
        assert s["text"].startswith("Herzlich willkommen zum Podcast")
        assert s["published_at"] == "2024-07-02" and s["kind"] == "audio"
        assert conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 1
    page = client.get(f"/s/{s['id']}").text
    assert "Transkript: automatische Untertitel (de)" in page


class FakeModel:
    def __init__(self, on_segment=None):
        self.on_segment = on_segment
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append(kw)
        info = SimpleNamespace(language="de", duration=10.0)

        def gen():
            for i in range(5):
                if self.on_segment:
                    self.on_segment(i)
                yield SimpleNamespace(start=i * 2.0, end=i * 2.0 + 2, text=f" Satz {i}.")
        return gen(), info


def test_subtitles_off_runs_whisper_with_chosen_model(client, fake_ytdlp, monkeypatch):
    used = []
    model = FakeModel()
    monkeypatch.setattr(tr, "_get_model", lambda name: used.append(name) or model)
    pid = _project(client)
    client.post(f"/p/{pid}/sources/url", data={
        "url": "https://youtube.com/watch?v=2", "options_shown": "1", "whisper_model": "base"})
    assert worker.drain() == 2
    with connection() as conn:
        s = conn.execute("SELECT * FROM sources").fetchone()
        jobs = conn.execute("SELECT type, options, status FROM jobs ORDER BY id").fetchall()
    assert used == ["base"]
    assert s["transcript_origin"] == "Whisper base" and s["state"] == "bereit"
    assert [j["type"] for j in jobs] == ["media", "transcribe"]
    assert '"subtitles": "off"' in jobs[1]["options"]


def test_progress_and_cancel(client, monkeypatch):
    monkeypatch.setattr("app.jobs.DEFAULT_INTERVAL", 0)
    pid = _project(client)
    with connection() as conn:
        sid = repo.create_source(conn, pid, "audio", title="lang", media_path="x.m4a")

    seen = []

    def on_segment(i):
        with connection() as conn:
            job = repo.active_job(conn, sid)
            seen.append(job["progress"])
            if i == 3:
                repo.request_cancel(conn, sid)

    monkeypatch.setattr(tr, "_get_model", lambda name: FakeModel(on_segment))
    client.post(f"/s/{sid}/run/transcribe", data={"whisper_model": "tiny"})
    state = client.get(f"/s/{sid}/state").json()
    assert state["state"] == "wartet" and state["job"] == "transcribe"
    worker.drain()
    assert seen[:3] == [0.0, 0.2, 0.4]
    with connection() as conn:
        s = repo.get_source(conn, sid)
        job = conn.execute("SELECT * FROM jobs").fetchone()
    assert job["status"] == "cancelled"
    assert s["state"] == "bereit" and s["error"] == "Abgebrochen" and s["text"] == ""
    assert "Verarbeitung abgebrochen" in client.get(f"/s/{sid}").text


def test_cancel_queued_job(client):
    pid = _project(client)
    with connection() as conn:
        sid = repo.create_source(conn, pid, "audio", title="x", media_path="x.m4a")
        repo.enqueue_job(conn, sid, "transcribe")
    client.post(f"/s/{sid}/run/cancel")
    assert worker.drain() == 0
    with connection() as conn:
        assert repo.get_source(conn, sid)["state"] == "bereit"


def test_heavy_and_light_lanes(app_env):
    with connection() as conn:
        pid = repo.create_project(conn, "P")
        a = repo.create_source(conn, pid, "audio", title="a")
        b = repo.create_source(conn, pid, "web", title="b", url="https://x")
        repo.enqueue_job(conn, a, "transcribe")
        repo.enqueue_job(conn, b, "web")
        assert repo.claim_next_job(conn, heavy=False)["type"] == "web"
        assert repo.claim_next_job(conn, heavy=False) is None
        assert repo.claim_next_job(conn, heavy=True)["type"] == "transcribe"


def test_running_job_reports_progress_in_list(client):
    pid = _project(client)
    with connection() as conn:
        sid = repo.create_source(conn, pid, "audio", title="lang")
        jid = repo.enqueue_job(conn, sid, "transcribe")
        repo.claim_next_job(conn, heavy=True)
        repo.set_job_progress(conn, jid, 0.37, 720, "Transkription mit „small“")
    html = client.get(f"/p/{pid}").text
    assert "Transkription mit „small“ · 37 % · noch ca. 12 Min." in html
    st = client.get(f"/s/{sid}/state").json()
    assert st["progress"] == 0.37 and st["eta_text"] == "noch ca. 12 Min."


def test_migration_from_v1(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "fundus.db")
    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    conn = sqlite3.connect(tmp_path / "fundus.db")
    conn.executescript("""
        CREATE TABLE sources (id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT, text TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, source_id INTEGER, type TEXT,
                           status TEXT NOT NULL DEFAULT 'queued', error TEXT, created_at TEXT,
                           started_at TEXT, finished_at TEXT);
        INSERT INTO jobs(source_id, type) VALUES (1, 'web');
    """)
    conn.close()
    db.init()
    with connection() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        assert {"options", "progress", "eta", "note", "cancel"} <= cols
        assert c.execute("SELECT options, cancel FROM jobs").fetchone()[:] == ("{}", 0)
        assert "transcript_origin" in {r["name"] for r in c.execute("PRAGMA table_info(sources)")}
