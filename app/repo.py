"""Datenzugriff: Projekte, Quellen, Transkripte, Markierungen, Tags, Suche, Jobs."""
from __future__ import annotations

import json
import shutil
import sqlite3
from typing import Any, Iterable

from . import config
from .util import fts_query, normalize_date, now_iso

KINDS = {
    "web": "Webseite",
    "video": "Video",
    "audio": "Audio / Podcast",
    "document": "Dokument",
    "image": "Bild",
    "note": "Notiz",
}
STATUSES = ["neu", "zu prüfen", "verifiziert", "verworfen"]
COLORS = {
    "gelb": "Wichtig",
    "grün": "Beleg",
    "blau": "Kontext",
    "rot": "Widerspruch",
    "lila": "Zitat",
}

SOURCE_EDITABLE = {
    "title", "url", "author", "site_name", "description", "language",
    "published_at", "event_date", "event_date_end", "status", "credibility",
    "notes", "text", "content_hash", "snapshot_path", "media_path",
    "mime_type", "duration", "state", "error", "kind", "transcript_origin",
}
DATE_FIELDS = {"published_at", "event_date", "event_date_end"}


# ---------------------------------------------------------------- Projekte

def create_project(conn: sqlite3.Connection, name: str, description: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("Projektname fehlt")
    cur = conn.execute(
        "INSERT INTO projects(name, description) VALUES (?, ?)", (name, description.strip())
    )
    return cur.lastrowid


def list_projects(conn: sqlite3.Connection, include_archived: bool = False) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT p.*,
               (SELECT COUNT(*) FROM sources s WHERE s.project_id = p.id) AS source_count,
               (SELECT COUNT(*) FROM highlights h JOIN sources s ON s.id = h.source_id
                 WHERE s.project_id = p.id) AS highlight_count,
               (SELECT MAX(s.captured_at) FROM sources s WHERE s.project_id = p.id) AS last_activity
        FROM projects p
        {'' if include_archived else 'WHERE p.archived = 0'}
        ORDER BY COALESCE(last_activity, p.created_at) DESC
        """
    ).fetchall()


def get_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def update_project(conn: sqlite3.Connection, project_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in {"name", "description", "archived"}}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", (*allowed.values(), project_id))


def delete_project(conn: sqlite3.Connection, project_id: int) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE project_id = ?", (project_id,))]
    for sid in ids:
        delete_source(conn, sid)
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------------------------------------------------------------- Quellen

def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in SOURCE_EDITABLE:
            continue
        if k in DATE_FIELDS:
            v = normalize_date(v) if v not in (None, "") else None
        elif k == "credibility":
            v = int(v) if v not in (None, "") else None
            if v is not None and not 1 <= v <= 5:
                raise ValueError("Glaubwürdigkeit muss zwischen 1 und 5 liegen")
        elif k == "status" and v not in STATUSES:
            raise ValueError(f"Unbekannter Status: {v}")
        elif k == "kind" and v not in KINDS:
            raise ValueError(f"Unbekannte Art: {v}")
        out[k] = v
    return out


def create_source(conn: sqlite3.Connection, project_id: int, kind: str, **fields: Any) -> int:
    if kind not in KINDS:
        raise ValueError(f"Unbekannte Art: {kind}")
    data = _clean_fields(fields)
    data["kind"] = kind
    cols = ["project_id", *data.keys()]
    cur = conn.execute(
        f"INSERT INTO sources({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        (project_id, *data.values()),
    )
    reindex_source(conn, cur.lastrowid)
    return cur.lastrowid


def update_source(conn: sqlite3.Connection, source_id: int, **fields: Any) -> None:
    data = _clean_fields(fields)
    if not data:
        return
    data["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE sources SET {sets} WHERE id = ?", (*data.values(), source_id))
    reindex_source(conn, source_id)


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def list_sources(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    order: str = "captured",
) -> list[sqlite3.Row]:
    where = ["s.project_id = ?"]
    args: list[Any] = [project_id]
    if kind:
        where.append("s.kind = ?")
        args.append(kind)
    if status:
        where.append("s.status = ?")
        args.append(status)
    if tag:
        where.append(
            "s.id IN (SELECT st.source_id FROM source_tags st JOIN tags t ON t.id = st.tag_id "
            "WHERE t.name = ?)"
        )
        args.append(tag)
    order_sql = {
        "captured": "s.captured_at DESC",
        "event": "COALESCE(s.event_date, s.published_at, substr(s.captured_at,1,10)) DESC",
        "title": "s.title COLLATE NOCASE",
    }.get(order, "s.captured_at DESC")
    return conn.execute(
        f"""
        SELECT s.id, s.project_id, s.kind, s.title, s.url, s.author, s.site_name,
               s.published_at, s.captured_at, s.event_date, s.event_date_end, s.status,
               s.credibility, s.state, s.error, s.duration,
               substr(s.description, 1, 300) AS description,
               (SELECT COUNT(*) FROM highlights h WHERE h.source_id = s.id) AS highlight_count
        FROM sources s
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql}
        """,
        args,
    ).fetchall()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM search_index WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    shutil.rmtree(source_dir(source_id), ignore_errors=True)


def source_dir(source_id: int):
    return config.FILES_DIR / str(source_id)


# ---------------------------------------------------------------- Tags

def parse_tags(raw: str | Iterable[str]) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    seen: dict[str, str] = {}
    for t in items:
        t = t.strip().lstrip("#").strip()
        if t and t.lower() not in seen:
            seen[t.lower()] = t
    return list(seen.values())


def _tag_ids(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    ids = []
    for n in names:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (n,))
        ids.append(conn.execute("SELECT id FROM tags WHERE name = ?", (n,)).fetchone()["id"])
    return ids


def set_source_tags(conn: sqlite3.Connection, source_id: int, names: str | list[str]) -> None:
    conn.execute("DELETE FROM source_tags WHERE source_id = ?", (source_id,))
    for tid in _tag_ids(conn, parse_tags(names)):
        conn.execute("INSERT OR IGNORE INTO source_tags VALUES (?, ?)", (source_id, tid))
    _cleanup_tags(conn)


def set_highlight_tags(conn: sqlite3.Connection, highlight_id: int, names: str | list[str]) -> None:
    conn.execute("DELETE FROM highlight_tags WHERE highlight_id = ?", (highlight_id,))
    for tid in _tag_ids(conn, parse_tags(names)):
        conn.execute("INSERT OR IGNORE INTO highlight_tags VALUES (?, ?)", (highlight_id, tid))
    _cleanup_tags(conn)


def _cleanup_tags(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM source_tags) "
        "AND id NOT IN (SELECT tag_id FROM highlight_tags)"
    )


def source_tags(conn: sqlite3.Connection, source_ids: list[int]) -> dict[int, list[str]]:
    if not source_ids:
        return {}
    rows = conn.execute(
        f"""SELECT st.source_id, t.name FROM source_tags st JOIN tags t ON t.id = st.tag_id
            WHERE st.source_id IN ({','.join('?' * len(source_ids))}) ORDER BY t.name COLLATE NOCASE""",
        source_ids,
    )
    out: dict[int, list[str]] = {i: [] for i in source_ids}
    for r in rows:
        out[r["source_id"]].append(r["name"])
    return out


def highlight_tags(conn: sqlite3.Connection, highlight_ids: list[int]) -> dict[int, list[str]]:
    if not highlight_ids:
        return {}
    rows = conn.execute(
        f"""SELECT ht.highlight_id, t.name FROM highlight_tags ht JOIN tags t ON t.id = ht.tag_id
            WHERE ht.highlight_id IN ({','.join('?' * len(highlight_ids))}) ORDER BY t.name COLLATE NOCASE""",
        highlight_ids,
    )
    out: dict[int, list[str]] = {i: [] for i in highlight_ids}
    for r in rows:
        out[r["highlight_id"]].append(r["name"])
    return out


def project_tags(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.name, COUNT(*) AS n FROM (
            SELECT st.tag_id FROM source_tags st JOIN sources s ON s.id = st.source_id
             WHERE s.project_id = ?
            UNION ALL
            SELECT ht.tag_id FROM highlight_tags ht JOIN highlights h ON h.id = ht.highlight_id
             JOIN sources s ON s.id = h.source_id WHERE s.project_id = ?
        ) x JOIN tags t ON t.id = x.tag_id
        GROUP BY t.id ORDER BY n DESC, t.name COLLATE NOCASE
        """,
        (project_id, project_id),
    ).fetchall()


# ---------------------------------------------------------------- Transkripte

def set_transcript(
    conn: sqlite3.Connection, source_id: int, segments: list[dict[str, Any]], language: str = "",
    origin: str | None = None,
) -> None:
    """Speichert Segmente und baut daraus den durchsuchbaren Text (eine Zeile je Segment)."""
    conn.execute("DELETE FROM transcript_segments WHERE source_id = ?", (source_id,))
    lines: list[str] = []
    offset = 0
    for i, seg in enumerate(segments):
        text = " ".join(str(seg["text"]).split())
        if not text:
            continue
        conn.execute(
            """INSERT INTO transcript_segments(source_id, idx, start, end, speaker, text, char_offset)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_id, i, float(seg["start"]), float(seg["end"]), seg.get("speaker"), text, offset),
        )
        lines.append(text)
        offset += len(text) + 1  # +1 für den Zeilenumbruch
    fields: dict[str, Any] = {"text": "\n".join(lines)}
    if origin:
        fields["transcript_origin"] = origin
    if language:
        fields["language"] = language
    update_source(conn, source_id, **fields)


def get_segments(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transcript_segments WHERE source_id = ? ORDER BY idx", (source_id,)
    ).fetchall()


def _time_at_offset(conn: sqlite3.Connection, source_id: int, offset: int) -> float | None:
    row = conn.execute(
        """SELECT start FROM transcript_segments WHERE source_id = ? AND char_offset <= ?
           ORDER BY char_offset DESC LIMIT 1""",
        (source_id, offset),
    ).fetchone()
    return row["start"] if row else None


# ---------------------------------------------------------------- Markierungen

def add_highlight(
    conn: sqlite3.Connection,
    source_id: int,
    start: int,
    end: int,
    note: str = "",
    color: str = "gelb",
    tags: str | list[str] = "",
    event_date: str | None = None,
) -> int:
    src = get_source(conn, source_id)
    if src is None:
        raise ValueError("Quelle nicht gefunden")
    text = src["text"]
    if not (0 <= start < end <= len(text)):
        raise ValueError("Ungültiger Textbereich")
    if color not in COLORS:
        color = "gelb"
    quote = text[start:end]
    cur = conn.execute(
        """INSERT INTO highlights(source_id, start_offset, end_offset, quote, note, color,
                                  media_time, event_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, start, end, quote, note.strip(), color,
         _time_at_offset(conn, source_id, start), normalize_date(event_date)),
    )
    hid = cur.lastrowid
    if tags:
        set_highlight_tags(conn, hid, tags)
    reindex_source(conn, source_id)
    return hid


def update_highlight(conn: sqlite3.Connection, highlight_id: int, **fields: Any) -> None:
    h = conn.execute("SELECT * FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if h is None:
        raise ValueError("Markierung nicht gefunden")
    data: dict[str, Any] = {}
    if "note" in fields:
        data["note"] = (fields["note"] or "").strip()
    if "color" in fields and fields["color"] in COLORS:
        data["color"] = fields["color"]
    if "event_date" in fields:
        data["event_date"] = normalize_date(fields["event_date"]) if fields["event_date"] else None
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        conn.execute(f"UPDATE highlights SET {sets} WHERE id = ?", (*data.values(), highlight_id))
    if "tags" in fields:
        set_highlight_tags(conn, highlight_id, fields["tags"] or "")
    reindex_source(conn, h["source_id"])


def delete_highlight(conn: sqlite3.Connection, highlight_id: int) -> None:
    h = conn.execute("SELECT source_id FROM highlights WHERE id = ?", (highlight_id,)).fetchone()
    if h:
        conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
        _cleanup_tags(conn)
        reindex_source(conn, h["source_id"])


def list_highlights(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM highlights WHERE source_id = ? ORDER BY start_offset", (source_id,)
    ).fetchall()


def project_highlights(
    conn: sqlite3.Connection, project_id: int, color: str | None = None, tag: str | None = None
) -> list[sqlite3.Row]:
    where = ["s.project_id = ?"]
    args: list[Any] = [project_id]
    if color:
        where.append("h.color = ?")
        args.append(color)
    if tag:
        where.append(
            "h.id IN (SELECT ht.highlight_id FROM highlight_tags ht JOIN tags t ON t.id = ht.tag_id "
            "WHERE t.name = ?)"
        )
        args.append(tag)
    return conn.execute(
        f"""SELECT h.*, s.title AS source_title, s.kind AS source_kind, s.url AS source_url,
                   s.author AS source_author, s.site_name AS source_site,
                   s.published_at AS source_published
            FROM highlights h JOIN sources s ON s.id = h.source_id
            WHERE {' AND '.join(where)}
            ORDER BY h.created_at DESC""",
        args,
    ).fetchall()


# ---------------------------------------------------------------- Zeitstrahl

def timeline(conn: sqlite3.Connection, project_id: int, basis: str = "event") -> list[dict[str, Any]]:
    """Einträge für den Zeitstrahl.

    basis = "event": inhaltliches Datum (Fallback: Veröffentlichung)
    basis = "published": Veröffentlichungsdatum
    basis = "captured": Erfassungsdatum
    Markierungen mit eigenem Datum erscheinen zusätzlich als eigene Einträge.
    """
    col = {
        "event": "COALESCE(s.event_date, s.published_at)",
        "published": "s.published_at",
        "captured": "substr(s.captured_at, 1, 10)",
    }.get(basis, "COALESCE(s.event_date, s.published_at)")
    entries: list[dict[str, Any]] = []
    for r in conn.execute(
        f"""SELECT s.id, s.kind, s.title, s.site_name, s.author, s.status, s.event_date_end,
                   s.event_date, s.published_at, {col} AS date,
                   (SELECT COUNT(*) FROM highlights h WHERE h.source_id = s.id) AS highlight_count
            FROM sources s WHERE s.project_id = ?""",
        (project_id,),
    ):
        entries.append({"type": "source", **dict(r)})
    if basis == "event":
        for r in conn.execute(
            """SELECT h.id, h.source_id, h.quote, h.note, h.color, h.event_date AS date,
                      h.media_time, s.title AS source_title, s.kind
               FROM highlights h JOIN sources s ON s.id = h.source_id
               WHERE s.project_id = ? AND h.event_date IS NOT NULL""",
            (project_id,),
        ):
            entries.append({"type": "highlight", **dict(r)})
    dated = sorted((e for e in entries if e["date"]), key=lambda e: e["date"])
    undated = [e for e in entries if not e["date"]]
    return dated + undated


# ---------------------------------------------------------------- Suche

def reindex_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM search_index WHERE source_id = ?", (source_id,))
    s = get_source(conn, source_id)
    if s is None:
        return
    pid = s["project_id"]
    segments = get_segments(conn, source_id)
    body_parts = [s["description"], s["notes"], s["author"], s["site_name"], s["url"] or ""]
    if not segments:
        body_parts.append(s["text"])
    conn.execute(
        "INSERT INTO search_index(title, body, kind, ref_id, source_id, project_id) VALUES (?,?,?,?,?,?)",
        (s["title"], "\n".join(p for p in body_parts if p), "source", source_id, source_id, pid),
    )
    for seg in segments:
        conn.execute(
            "INSERT INTO search_index(title, body, kind, ref_id, source_id, project_id) VALUES (?,?,?,?,?,?)",
            (s["title"], seg["text"], "segment", seg["id"], source_id, pid),
        )
    hl = list_highlights(conn, source_id)
    tags = highlight_tags(conn, [h["id"] for h in hl])
    for h in hl:
        conn.execute(
            "INSERT INTO search_index(title, body, kind, ref_id, source_id, project_id) VALUES (?,?,?,?,?,?)",
            (s["title"], "\n".join([h["quote"], h["note"], " ".join(tags.get(h["id"], []))]),
             "highlight", h["id"], source_id, pid),
        )


def search(
    conn: sqlite3.Connection, q: str, project_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    match = fts_query(q)
    if not match:
        return []
    where = "search_index MATCH ?"
    args: list[Any] = [match]
    if project_id:
        where += " AND project_id = ?"
        args.append(project_id)
    rows = conn.execute(
        f"""SELECT kind, ref_id, source_id, project_id,
                   highlight(search_index, 0, '[[', ']]') AS title_hl,
                   snippet(search_index, 1, '[[', ']]', ' … ', 24) AS snippet,
                   bm25(search_index, 4.0, 1.0) AS score
            FROM search_index WHERE {where}
            ORDER BY score LIMIT ?""",
        (*args, limit),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        src = conn.execute(
            "SELECT title, kind, project_id FROM sources WHERE id = ?", (r["source_id"],)
        ).fetchone()
        if src is None:
            continue
        item["source_title"] = src["title"]
        item["source_kind"] = src["kind"]
        proj = get_project(conn, src["project_id"])
        item["project_name"] = proj["name"] if proj else ""
        if r["kind"] == "segment":
            seg = conn.execute(
                "SELECT start FROM transcript_segments WHERE id = ?", (r["ref_id"],)
            ).fetchone()
            item["time"] = seg["start"] if seg else None
        elif r["kind"] == "highlight":
            h = conn.execute("SELECT color, media_time FROM highlights WHERE id = ?", (r["ref_id"],)).fetchone()
            item["color"] = h["color"] if h else None
            item["time"] = h["media_time"] if h else None
        results.append(item)
    return results


# ---------------------------------------------------------------- Jobs

# Transkriptionen sind langsam (CPU) und laufen deshalb in einer eigenen Spur,
# damit Webseiten und Downloads nicht stundenlang warten müssen.
HEAVY_JOBS = ("transcribe",)


def enqueue_job(
    conn: sqlite3.Connection, source_id: int, job_type: str, options: dict[str, Any] | None = None
) -> int:
    conn.execute("UPDATE sources SET state = 'wartet', error = NULL WHERE id = ?", (source_id,))
    return conn.execute(
        "INSERT INTO jobs(source_id, type, options) VALUES (?, ?, ?)",
        (source_id, job_type, json.dumps(options or {})),
    ).lastrowid


def claim_next_job(
    conn: sqlite3.Connection, heavy: bool | None = None
) -> sqlite3.Row | None:
    """Nächsten Job holen. heavy=True nur Transkriptionen, False alles andere, None beides."""
    where = "status = 'queued'"
    ph = ",".join("?" * len(HEAVY_JOBS))
    args: list[Any] = []
    if heavy is True:
        where += f" AND type IN ({ph})"
        args += HEAVY_JOBS
    elif heavy is False:
        where += f" AND type NOT IN ({ph})"
        args += HEAVY_JOBS
    row = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY id LIMIT 1", args).fetchone()
    if row is None:
        return None
    updated = conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
        (now_iso(), row["id"]),
    ).rowcount
    if not updated:
        return None
    conn.execute("UPDATE sources SET state = 'läuft' WHERE id = ?", (row["source_id"],))
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def set_job_progress(
    conn: sqlite3.Connection, job_id: int, progress: float | None, eta: float | None, note: str
) -> None:
    conn.execute(
        "UPDATE jobs SET progress = ?, eta = ?, note = ? WHERE id = ?", (progress, eta, note, job_id)
    )


def job_cancel_requested(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT cancel FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel"])


def request_cancel(conn: sqlite3.Connection, source_id: int) -> None:
    """Wartende Jobs sofort beenden, laufende zum Abbruch markieren."""
    conn.execute(
        """UPDATE jobs SET status = 'cancelled', error = 'Abgebrochen', finished_at = ?
           WHERE source_id = ? AND status = 'queued'""",
        (now_iso(), source_id),
    )
    conn.execute(
        "UPDATE jobs SET cancel = 1 WHERE source_id = ? AND status = 'running'", (source_id,)
    )
    _refresh_source_state(conn, source_id, cancelled=True)


def _refresh_source_state(conn: sqlite3.Connection, source_id: int, error: str | None = None,
                          cancelled: bool = False) -> None:
    counts = {r["status"]: r["n"] for r in conn.execute(
        """SELECT status, COUNT(*) AS n FROM jobs WHERE source_id = ?
           AND status IN ('queued','running') GROUP BY status""", (source_id,))}
    if error:
        conn.execute("UPDATE sources SET state = 'fehler', error = ? WHERE id = ?", (error, source_id))
    elif counts.get("running"):
        conn.execute("UPDATE sources SET state = 'läuft' WHERE id = ?", (source_id,))
    elif counts.get("queued"):
        conn.execute("UPDATE sources SET state = 'wartet' WHERE id = ?", (source_id,))
    else:
        conn.execute(
            "UPDATE sources SET state = 'bereit', error = ? WHERE id = ?",
            ("Abgebrochen" if cancelled else None, source_id),
        )


def finish_job(
    conn: sqlite3.Connection, job_id: int, error: str | None = None, cancelled: bool = False
) -> None:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    status = "cancelled" if cancelled else ("failed" if error else "done")
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, finished_at = ?, eta = NULL WHERE id = ?",
        (status, "Abgebrochen" if cancelled else error, now_iso(), job_id),
    )
    if job is not None:
        _refresh_source_state(conn, job["source_id"], None if cancelled else error, cancelled)


def requeue_stale_jobs(conn: sqlite3.Connection) -> None:
    """Nach einem Neustart unterbrochene Jobs wieder einreihen (abgebrochene beenden)."""
    conn.execute(
        """UPDATE jobs SET status = 'cancelled', error = 'Abgebrochen', finished_at = ?
           WHERE status = 'running' AND cancel = 1""", (now_iso(),))
    conn.execute(
        """UPDATE jobs SET status = 'queued', started_at = NULL, progress = NULL, eta = NULL, note = NULL
           WHERE status = 'running'""")
    conn.execute(
        "UPDATE sources SET state = 'wartet' WHERE id IN (SELECT source_id FROM jobs WHERE status = 'queued')"
    )


def active_job(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM jobs WHERE source_id = ? AND status IN ('running','queued')
           ORDER BY (status = 'running') DESC, id LIMIT 1""",
        (source_id,),
    ).fetchone()


def active_jobs_for_project(conn: sqlite3.Connection, project_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        """SELECT j.* FROM jobs j JOIN sources s ON s.id = j.source_id
           WHERE s.project_id = ? AND j.status IN ('running','queued')
           ORDER BY (j.status = 'running'), j.id DESC""",
        (project_id,),
    ).fetchall()
    return {r["source_id"]: r for r in rows}  # laufende Jobs überschreiben wartende


def source_jobs(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE source_id = ? ORDER BY id DESC LIMIT 10", (source_id,)
    ).fetchall()
