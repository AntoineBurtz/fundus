import pytest

from app import repo
from app.db import connection
from app.render import render_blocks, resolve_highlights
from app.util import fts_query, human_date, normalize_date, timecode


# ------------------------------------------------------------- util

@pytest.mark.parametrize("raw,expected", [
    ("2019", "2019"),
    ("2019-3", "2019-03"),
    ("2019-03-12", "2019-03-12"),
    ("12.03.2019", "2019-03-12"),
    ("3.2019", "2019-03"),
    ("20190312", "2019-03-12"),
    ("2024-03-05T10:00:00Z", "2024-03-05"),
    ("", None),
    (None, None),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_normalize_date_invalid():
    with pytest.raises(ValueError):
        normalize_date("31.02.2020")
    with pytest.raises(ValueError):
        normalize_date("gestern")


def test_human_date_and_timecode():
    assert human_date("2019") == "2019"
    assert human_date("2019-03") == "März 2019"
    assert human_date("2019-03-05") == "5. März 2019"
    assert timecode(65) == "1:05"
    assert timecode(3725) == "1:02:05"


def test_fts_query():
    assert fts_query("Genehm Lech") == '"Genehm"* "Lech"*'
    assert fts_query('"Wasser recht" -Bayern') == '"Wasser recht" NOT "Bayern"*'
    assert fts_query("a OR b") == '"a"* OR "b"*'
    assert fts_query("-nur") == ""
    assert fts_query('weird"(*)') == '"weird"*'


# ------------------------------------------------------------- repo

def _project(conn):
    return repo.create_project(conn, "Testrecherche", "Beschreibung")


def test_source_highlight_search_cycle(app_env):
    with connection() as conn:
        pid = _project(conn)
        text = "Erster Absatz über den Lech.\nZweiter Absatz: Die Genehmigung kam 2019."
        sid = repo.create_source(conn, pid, "note", title="Notiz", text=text, event_date="03.2019")
        start = text.index("Genehmigung")
        hid = repo.add_highlight(conn, sid, start, start + len("Genehmigung kam 2019"),
                                 note="wichtig", color="grün", tags="Behörde, #Lech",
                                 event_date="15.04.2019")
        h = conn.execute("SELECT * FROM highlights WHERE id = ?", (hid,)).fetchone()
        assert h["quote"] == "Genehmigung kam 2019"
        assert h["event_date"] == "2019-04-15"
        assert repo.highlight_tags(conn, [hid])[hid] == ["Behörde", "Lech"]

        kinds = {r["kind"] for r in repo.search(conn, "genehm")}
        assert {"source", "highlight"} <= kinds
        assert repo.search(conn, "wichtig")[0]["kind"] == "highlight"
        assert repo.search(conn, "genehm", project_id=pid + 99) == []

        tl = repo.timeline(conn, pid)
        assert [e["type"] for e in tl] == ["source", "highlight"]
        assert tl[0]["date"] == "2019-03"

        with pytest.raises(ValueError):
            repo.add_highlight(conn, sid, 10, 5)


def test_transcript_offsets_and_media_time(app_env):
    with connection() as conn:
        pid = _project(conn)
        sid = repo.create_source(conn, pid, "audio", title="Podcast")
        repo.set_transcript(conn, sid, [
            {"start": 0.0, "end": 4.0, "text": " Hallo und willkommen. "},
            {"start": 4.0, "end": 9.5, "text": "Heute geht es um Wasserrechte."},
            {"start": 9.5, "end": 12.0, "text": "   "},
            {"start": 12.0, "end": 15.0, "text": "Ende."},
        ], language="de")
        s = repo.get_source(conn, sid)
        assert s["text"] == "Hallo und willkommen.\nHeute geht es um Wasserrechte.\nEnde."
        assert s["language"] == "de"
        start = s["text"].index("Wasserrechte")
        hid = repo.add_highlight(conn, sid, start, start + 12)
        assert conn.execute("SELECT media_time FROM highlights WHERE id=?", (hid,)).fetchone()[0] == 4.0
        hits = repo.search(conn, "wasserrecht")
        seg_hits = [r for r in hits if r["kind"] == "segment"]
        assert seg_hits and seg_hits[0]["time"] == 4.0
        # Quelltext selbst wird bei Transkripten nicht doppelt indiziert
        assert not [r for r in hits if r["kind"] == "source"]


def test_tags_filter_and_cleanup(app_env):
    with connection() as conn:
        pid = _project(conn)
        a = repo.create_source(conn, pid, "note", title="A")
        b = repo.create_source(conn, pid, "note", title="B")
        repo.set_source_tags(conn, a, "Lech, Wasser, lech")
        repo.set_source_tags(conn, b, "Wasser")
        assert [s["title"] for s in repo.list_sources(conn, pid, tag="Lech")] == ["A"]
        assert {t["name"]: t["n"] for t in repo.project_tags(conn, pid)} == {"Wasser": 2, "Lech": 1}
        repo.set_source_tags(conn, a, "")
        assert {t["name"] for t in repo.project_tags(conn, pid)} == {"Wasser"}
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1


def test_validation(app_env):
    with connection() as conn:
        pid = _project(conn)
        with pytest.raises(ValueError):
            repo.create_source(conn, pid, "unsinn")
        sid = repo.create_source(conn, pid, "note", title="x")
        with pytest.raises(ValueError):
            repo.update_source(conn, sid, credibility=9)
        with pytest.raises(ValueError):
            repo.update_source(conn, sid, status="egal")


# ------------------------------------------------------------- render

def test_render_overlapping_and_escaping():
    text = "Eins <b>zwei</b> drei\nvier"
    hls = [
        {"id": 1, "start_offset": 0, "end_offset": 12, "quote": text[0:12], "color": "gelb"},
        {"id": 2, "start_offset": 5, "end_offset": 20, "quote": text[5:20], "color": "grün"},
    ]
    blocks = render_blocks(text, hls)
    assert [b["offset"] for b in blocks] == [0, 22]
    html = blocks[0]["html"]
    assert "&lt;b&gt;" in html and "<b>" not in html
    assert 'data-ids="1,2"' in html and "hl-multi" in html
    assert 'class="hl c-gruen" data-ids="2"' in html
    # Zusammengesetzter Text bleibt unverändert
    import re
    from html import unescape
    assert unescape(re.sub(r"<[^>]+>", "", html)) == "Eins <b>zwei</b> drei"


def test_resolve_moved_and_orphaned():
    text = "neu: Hallo Welt und mehr"
    ok, orphans = resolve_highlights(text, [
        {"id": 1, "start_offset": 0, "end_offset": 11, "quote": "Hallo Welt"},
        {"id": 2, "start_offset": 0, "end_offset": 3, "quote": "weg"},
    ])
    assert ok[0]["start_offset"] == 5 and ok[0]["end_offset"] == 15
    assert [o["id"] for o in orphans] == [2]
