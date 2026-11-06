"""Beobachtete Social-Media-Accounts und Cookies der Recherche-Konten."""
from __future__ import annotations

import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .util import now_iso

PLATFORMS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
}
# Welche Inhalte pro Plattform angeboten werden
CONTENT = {
    "instagram": {"stories": "Stories", "highlights": "Highlights", "posts": "Beiträge & Reels"},
    "tiktok": {"stories": "Stories", "posts": "Videos"},
}
INTERVALS = {1: "stündlich", 2: "alle 2 Stunden", 3: "alle 3 Stunden", 6: "alle 6 Stunden",
             12: "alle 12 Stunden"}
# Cookie, an dem eine gültige Anmeldung erkennbar ist
SESSION_COOKIE = {"instagram": "sessionid", "tiktok": "sessionid"}
DOMAIN = {"instagram": "instagram.com", "tiktok": "tiktok.com"}

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,40}$")


def normalize_handle(platform: str, raw: str) -> str:
    """Akzeptiert @name, name oder eine Profil-URL."""
    h = (raw or "").strip()
    h = re.sub(r"^https?://(www\.)?(instagram\.com|tiktok\.com)/", "", h)
    h = h.split("?")[0].strip("/").split("/")[0].lstrip("@")
    if platform not in PLATFORMS:
        raise ValueError("Unbekannte Plattform")
    if not _HANDLE_RE.match(h):
        raise ValueError(f"Ungültiger Account-Name: {raw!r}")
    return h.lower()


def profile_url(platform: str, handle: str) -> str:
    return (f"https://www.instagram.com/{handle}/" if platform == "instagram"
            else f"https://www.tiktok.com/@{handle}")


def content_urls(acc) -> list[tuple[str, str]]:
    """(Art, URL) für alle ausgewählten Inhalte eines Accounts."""
    h = acc["handle"]
    out = []
    if acc["platform"] == "instagram":
        if acc["stories"]:
            out.append(("stories", f"https://www.instagram.com/stories/{h}/"))
        if acc["highlights"]:
            out.append(("highlights", f"https://www.instagram.com/{h}/highlights/"))
        if acc["posts"]:
            out.append(("posts", f"https://www.instagram.com/{h}/posts/"))
    else:
        if acc["stories"]:
            out.append(("stories", f"https://www.tiktok.com/@{h}/stories"))
        if acc["posts"]:
            out.append(("posts", f"https://www.tiktok.com/@{h}/posts"))
    return out


# ------------------------------------------------------------- Accounts

def _next_check(hours: float) -> str:
    # ±20 % Zufall, damit die Abrufe nicht im starren Takt kommen
    jitter = hours * 3600 * random.uniform(0.8, 1.2)
    return (datetime.now(timezone.utc) + timedelta(seconds=jitter)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_account(conn: sqlite3.Connection, project_id: int, platform: str, handle: str, *,
                stories: bool = True, highlights: bool = False, posts: bool = False,
                transcribe: bool = True, interval_hours: float = 3) -> int:
    handle = normalize_handle(platform, handle)
    if platform == "tiktok":
        highlights = False
    if not (stories or highlights or posts):
        raise ValueError("Bitte mindestens eine Inhaltsart auswählen")
    try:
        return conn.execute(
            """INSERT INTO watched_accounts(project_id, platform, handle, stories, highlights, posts,
                                            transcribe, interval_hours, next_check_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, platform, handle, int(stories), int(highlights), int(posts),
             int(transcribe), float(interval_hours), now_iso()),
        ).lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError(f"@{handle} wird in dieser Recherche bereits beobachtet") from e


def get_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM watched_accounts WHERE id = ?", (account_id,)).fetchone()


def list_accounts(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*,
                  (SELECT COUNT(*) FROM sources s WHERE s.account_id = a.id) AS sources,
                  (SELECT MAX(s.published_at) FROM sources s WHERE s.account_id = a.id) AS last_item,
                  (SELECT status FROM jobs j WHERE j.type = 'account'
                     AND json_extract(j.options, '$.account_id') = a.id
                     AND j.status IN ('queued','running')
                     ORDER BY (j.status = 'running') DESC LIMIT 1) AS job_status,
                  (SELECT note FROM jobs j WHERE j.type = 'account'
                     AND json_extract(j.options, '$.account_id') = a.id
                     AND j.status = 'running' LIMIT 1) AS job_note,
                  (SELECT COUNT(*) FROM jobs j WHERE j.type = 'account'
                     AND json_extract(j.options, '$.account_id') = a.id
                     AND json_extract(j.options, '$.backfill') IS NOT NULL
                     AND j.status IN ('queued','running')) AS backfill_pending
           FROM watched_accounts a WHERE a.project_id = ? ORDER BY a.platform, a.handle""",
        (project_id,),
    ).fetchall()


def update_account(conn: sqlite3.Connection, account_id: int, **fields) -> None:
    allowed = {"stories", "highlights", "posts", "transcribe", "interval_hours", "active"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE watched_accounts SET {sets} WHERE id = ?", (*data.values(), account_id))


def delete_account(conn: sqlite3.Connection, account_id: int) -> None:
    """Beobachtung beenden. Bereits gesicherte Inhalte bleiben als Quellen erhalten."""
    conn.execute("UPDATE sources SET account_id = NULL WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM watched_accounts WHERE id = ?", (account_id,))


def enqueue_check(conn: sqlite3.Connection, account_id: int) -> int | None:
    busy = conn.execute(
        """SELECT 1 FROM jobs WHERE type = 'account' AND status IN ('queued','running')
           AND options = ?""", (_opts(account_id),)).fetchone()
    if busy:
        return None
    return conn.execute(
        "INSERT INTO jobs(source_id, type, options) VALUES (NULL, 'account', ?)", (_opts(account_id),)
    ).lastrowid


def enqueue_backfill(conn: sqlite3.Connection, account_id: int, *, stories: bool = False,
                     highlights: bool = False, posts: bool = False,
                     since: str | None = None) -> int:
    """Einmaliger Archiv-Auftrag: aktuelle Stories, Highlights und/oder bisherige Beiträge.

    since = "JJJJ-MM-TT" lädt Beiträge ab diesem Tag, None = alle.
    """
    import json

    if not (stories or highlights or posts):
        raise ValueError("Bitte auswählen, was nachgeladen werden soll")
    if since:
        since = datetime.fromisoformat(since).strftime("%Y-%m-%d")
    opts = {"account_id": account_id,
            "backfill": {"stories": bool(stories), "highlights": bool(highlights),
                         "posts": bool(posts), "since": since}}
    return conn.execute(
        "INSERT INTO jobs(source_id, type, options) VALUES (NULL, 'account', ?)", (json.dumps(opts),)
    ).lastrowid


def _opts(account_id: int) -> str:
    return '{"account_id": %d}' % account_id


def enqueue_due_accounts(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT id, interval_hours FROM watched_accounts
           WHERE active = 1 AND (next_check_at IS NULL OR next_check_at <= ?)""", (now_iso(),)
    ).fetchall()
    n = 0
    for r in rows:
        conn.execute("UPDATE watched_accounts SET next_check_at = ? WHERE id = ?",
                     (_next_check(r["interval_hours"]), r["id"]))
        if enqueue_check(conn, r["id"]):
            n += 1
    return n


def record_check(conn: sqlite3.Connection, account_id: int, status: str, error: str | None,
                 new_items: int) -> None:
    conn.execute(
        """UPDATE watched_accounts SET last_check_at = ?, last_status = ?, last_error = ?,
                  item_count = item_count + ? WHERE id = ?""",
        (now_iso(), status, error, new_items, account_id),
    )


# ------------------------------------------------------------- Cookies

def cookie_path(platform: str) -> Path:
    return config.DATA_DIR / "cookies" / f"{platform}.txt"


def save_cookies(platform: str, data: bytes) -> dict:
    """Speichert eine cookies.txt (Netscape-Format) und prüft sie grob."""
    if platform not in PLATFORMS:
        raise ValueError("Unbekannte Plattform")
    text = data.decode("utf-8", "replace")
    info = inspect_cookies(platform, text)
    if not info["lines"]:
        raise ValueError("Keine Cookies im Netscape-Format gefunden (cookies.txt erwartet)")
    if not info["domain_ok"]:
        raise ValueError(f"Die Datei enthält keine Cookies für {DOMAIN[platform]}")
    path = cookie_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return info


def inspect_cookies(platform: str, text: str) -> dict:
    lines = [l for l in text.splitlines()
             if (not l.startswith("#") or l.startswith("#HttpOnly_")) and len(l.split("\t")) >= 7]
    domain_ok = False
    session = False
    expires = None
    for l in lines:
        parts = l.split("\t")
        if len(parts) < 7:
            continue
        if DOMAIN[platform] in parts[0]:
            domain_ok = True
            if parts[5] == SESSION_COOKIE[platform]:
                session = True
                try:
                    exp = int(parts[4])
                    if exp > 0:
                        expires = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    pass
    return {"lines": len(lines), "domain_ok": domain_ok, "session": session, "expires": expires}


def cookie_status(platform: str) -> dict:
    path = cookie_path(platform)
    if not path.is_file():
        return {"present": False}
    info = inspect_cookies(platform, path.read_text(encoding="utf-8", errors="replace"))
    info["present"] = True
    info["updated"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


def delete_cookies(platform: str) -> None:
    cookie_path(platform).unlink(missing_ok=True)

