"""Änderungsüberwachung, Neusicherung, Zeitstempel-Job, archive.org."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .. import config, evidence, repo
from ..db import connection
from ..jobs import JobContext
from ..util import now_iso
from .web import extract_html

log = logging.getLogger("fundus.watch")

WATCH_CHOICES = {0: "Aus", 6: "alle 6 Stunden", 24: "täglich", 168: "wöchentlich"}


def _later(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_watch(conn, source_id: int, hours: int | None) -> None:
    if hours:
        conn.execute(
            "UPDATE sources SET watch_hours = ?, next_check_at = ? WHERE id = ?",
            (hours, _later(hours), source_id),
        )
    else:
        conn.execute(
            "UPDATE sources SET watch_hours = NULL, next_check_at = NULL WHERE id = ?", (source_id,)
        )


def enqueue_due_checks(conn) -> int:
    """Fällige Prüfungen einreihen (vom Worker regelmäßig aufgerufen)."""
    rows = conn.execute(
        """SELECT id, watch_hours FROM sources
           WHERE watch_hours IS NOT NULL AND url IS NOT NULL AND next_check_at <= ?
             AND id NOT IN (SELECT source_id FROM jobs WHERE type = 'recheck'
                            AND status IN ('queued', 'running'))""",
        (now_iso(),),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE sources SET next_check_at = ? WHERE id = ?",
                     (_later(r["watch_hours"]), r["id"]))
        repo.enqueue_job(conn, r["id"], "recheck", {"scheduled": True})
    return len(rows)


def recheck(source_id: int, job: JobContext):
    """Seite neu abrufen. Neue Fassung nur bei Änderung (oder wenn erzwungen)."""
    force = bool(job.options.get("force"))
    with connection() as conn:
        src = repo.get_source(conn, source_id)
        last = evidence.latest_content_version(conn, source_id)
        prev_status = src["watch_status"]
    job.progress(None, "Seite erneut abrufen", force=True)
    try:
        resp = httpx.get(src["url"], follow_redirects=True, timeout=45,
                         headers={"User-Agent": config.USER_AGENT, "Accept-Language": "de,en;q=0.8"})
    except httpx.HTTPError as e:
        with connection() as conn:
            conn.execute("UPDATE sources SET watch_status = 'nicht erreichbar', last_check_at = ? WHERE id = ?",
                         (now_iso(), source_id))
        if force:
            raise RuntimeError(f"Seite nicht erreichbar: {e}") from e
        return None

    if resp.status_code in (404, 410):
        with connection() as conn:
            conn.execute("UPDATE sources SET watch_status = 'gelöscht', last_check_at = ? WHERE id = ?",
                         (now_iso(), source_id))
            if prev_status != "gelöscht":
                evidence.create_version(conn, source_id, "gelöscht", http_status=resp.status_code,
                                        final_url=str(resp.url))
                _alert(conn, source_id)
        if prev_status != "gelöscht":
            notify(src, f"Seite nicht mehr abrufbar (HTTP {resp.status_code})")
        return None
    if resp.status_code >= 400:
        with connection() as conn:
            conn.execute("UPDATE sources SET watch_status = 'nicht erreichbar', last_check_at = ? WHERE id = ?",
                         (now_iso(), source_id))
        if force:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return None

    ctype = resp.headers.get("content-type", "").split(";")[0].lower()
    is_html = "html" in ctype or not ctype
    text = extract_html(resp.text, str(resp.url))["text"] if is_html else ""
    if not is_html:
        import hashlib
        text = f"[Datei {ctype}, SHA-256 {hashlib.sha256(resp.content).hexdigest()}]"
    if not text.strip() and last is not None and last["text"] and not force:
        # Leere Seite statt Inhalt: meist Bot-Sperre/Captcha – keine Fassung anlegen
        with connection() as conn:
            conn.execute("UPDATE sources SET watch_status = 'kein Text (evtl. gesperrt)', "
                         "last_check_at = ? WHERE id = ?", (now_iso(), source_id))
        return None
    changed = last is None or evidence.text_hash(text) != last["text_hash"]
    with connection() as conn:
        conn.execute("UPDATE sources SET last_check_at = ?, watch_status = ? WHERE id = ?",
                     (now_iso(), "geändert" if changed and last is not None else "unverändert", source_id))
        if not changed and not force:
            return None
        reason = "manuell" if force and not changed else ("änderung" if last is not None else "erstsicherung")
        vid, n, folder = evidence.create_version(
            conn, source_id, reason, http_status=resp.status_code, final_url=str(resp.url),
            title=src["title"], text=text)
        name = "page.html" if is_html else "original" + (f".{ctype.split('/')[-1]}" if "/" in ctype else "")
        path = folder / name
        path.write_bytes(resp.content)
        evidence.add_artifact(conn, source_id, vid, "html" if is_html else "original", path, ctype or None)
        if changed and last is not None:
            _alert(conn, source_id)
    if changed and last is not None:
        notify(src, f"Inhalt hat sich geändert (Fassung {n})")
    return [("capture", {"version_id": vid})] if is_html else [("seal", {"version_id": vid})]


def _alert(conn, source_id: int) -> None:
    conn.execute("UPDATE sources SET watch_alert = 1, last_change_at = ? WHERE id = ?",
                 (now_iso(), source_id))


def notify(src, message: str) -> None:
    """Benachrichtigung an ntfy oder einen JSON-Webhook (z. B. Home Assistant)."""
    if not config.NOTIFY_URL:
        return
    title = f"Fundus: {src['title'] or src['url']}"[:200]
    link = f"{config.BASE_URL}/s/{src['id']}" if config.BASE_URL else (src["url"] or "")
    try:
        if "ntfy" in config.NOTIFY_URL:
            headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": "mag"}
            if link:
                headers["Click"] = link
            httpx.post(config.NOTIFY_URL, content=message.encode(), headers=headers, timeout=15)
        else:
            httpx.post(config.NOTIFY_URL, json={"title": title, "message": message, "url": link,
                                                "source_id": src["id"]}, timeout=15)
    except httpx.HTTPError as e:
        log.warning("Benachrichtigung fehlgeschlagen: %s", e)


def seal_job(source_id: int, job: JobContext):
    version_id = job.options.get("version_id")
    if version_id:
        job.progress(None, "Zeitstempel anfordern", force=True)
        evidence.seal(connection, version_id)
    return None


def archive_org(source_id: int, job: JobContext):
    """Seite über „Save Page Now“ bei archive.org sichern (öffentlich sichtbar!)."""
    with connection() as conn:
        src = repo.get_source(conn, source_id)
    job.progress(None, "Sicherung bei archive.org (kann bis zu 2 Minuten dauern)", force=True)
    r = httpx.get("https://web.archive.org/save/" + quote(src["url"], safe=":/?&=%#"),
                  timeout=180, follow_redirects=False,
                  headers={"User-Agent": "Fundus (self-hosted research archive)"})
    loc = r.headers.get("content-location") or r.headers.get("location") or ""
    if loc.startswith("/web/"):
        loc = "https://web.archive.org" + loc
    if not loc.startswith("https://web.archive.org/web/"):
        # Rückfall: neueste vorhandene Sicherung abfragen
        a = httpx.get("https://archive.org/wayback/available", params={"url": src["url"]}, timeout=30)
        snap = (a.json().get("archived_snapshots") or {}).get("closest") or {}
        loc = snap.get("url") or ""
        if not loc:
            raise RuntimeError(f"archive.org hat die Seite nicht gesichert (HTTP {r.status_code})")
    with connection() as conn:
        conn.execute("UPDATE sources SET archive_url = ? WHERE id = ?", (loc, source_id))
    return None
