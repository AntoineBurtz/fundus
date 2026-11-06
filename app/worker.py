"""Hintergrund-Worker mit zwei Spuren.

- "schnell": Webseiten, Datei-Uploads, Medien-Downloads
- "langsam": Transkriptionen (auf CPU oft viele Minuten)

So wartet eine eben gesicherte Webseite nie hinter einem einstündigen Podcast.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback

from . import repo
from .db import connection
from .ingest.capture import capture
from .ingest.files import ingest_file
from .ingest.media import ingest_media
from .ingest.transcribe import transcribe
from .ingest.watch import archive_org, enqueue_due_checks, recheck, seal_job
from .ingest.web import ingest_web
from .jobs import Cancelled, JobContext

log = logging.getLogger("fundus.worker")

HANDLERS = {
    "web": ingest_web,
    "media": ingest_media,
    "file": ingest_file,
    "transcribe": transcribe,
    "capture": capture,
    "seal": seal_job,
    "recheck": recheck,
    "archive": archive_org,
}

_stop = threading.Event()
_wake = threading.Event()


def run_one(heavy: bool | None = None) -> bool:
    """Bearbeitet einen Job. Gibt False zurück, wenn keiner anstand."""
    with connection() as conn:
        row = repo.claim_next_job(conn, heavy)
    if row is None:
        return False
    job = JobContext.from_row(row)
    handler = HANDLERS.get(row["type"])
    error = None
    cancelled = False
    follow_up = None
    try:
        if handler is None:
            raise RuntimeError(f"Unbekannter Jobtyp: {row['type']}")
        log.info("Job %s (%s) für Quelle %s", row["id"], row["type"], row["source_id"])
        follow_up = handler(row["source_id"], job)
    except Cancelled:
        log.info("Job %s abgebrochen", row["id"])
        cancelled = True
    except Exception as e:  # Fehler am Job speichern, Worker läuft weiter
        log.warning("Job %s fehlgeschlagen: %s", row["id"], e)
        log.debug(traceback.format_exc())
        error = f"{type(e).__name__}: {e}"[:2000]
    with connection() as conn:
        if follow_up and not error and not cancelled:
            for ftype, fopts in _normalize(follow_up, job.options):
                repo.enqueue_job(conn, row["source_id"], ftype, fopts)
        repo.finish_job(conn, row["id"], error, cancelled)
    if follow_up:
        wake()
    return True


def _normalize(follow_up, options: dict) -> list[tuple[str, dict]]:
    """Folge-Jobs: "typ", ("typ", optionen) oder eine Liste davon."""
    items = follow_up if isinstance(follow_up, list) else [follow_up]
    out = []
    for item in items:
        if isinstance(item, tuple):
            out.append((item[0], item[1] if item[1] is not None else options))
        elif item:
            out.append((item, options))
    return out


_last_schedule = 0.0


def check_schedule(force: bool = False) -> int:
    """Höchstens einmal pro Minute fällige Überwachungs-Prüfungen einreihen."""
    global _last_schedule
    now = time.monotonic()
    if not force and now - _last_schedule < 60:
        return 0
    _last_schedule = now
    with connection() as conn:
        return enqueue_due_checks(conn)


def _loop(heavy: bool) -> None:
    while not _stop.is_set():
        try:
            if not heavy:
                check_schedule()
            worked = run_one(heavy)
        except Exception:  # pragma: no cover
            log.exception("Worker-Fehler")
            worked = False
        if not worked:
            _wake.wait(3)
            _wake.clear()


def wake() -> None:
    _wake.set()


def start() -> list[threading.Thread]:
    with connection() as conn:
        repo.requeue_stale_jobs(conn)
    threads = [
        threading.Thread(target=_loop, args=(False,), name="fundus-schnell", daemon=True),
        threading.Thread(target=_loop, args=(True,), name="fundus-langsam", daemon=True),
    ]
    for t in threads:
        t.start()
    return threads


def stop() -> None:
    _stop.set()
    _wake.set()


def drain(max_jobs: int = 100) -> int:
    """Alle anstehenden Jobs synchron abarbeiten (für Tests und CLI)."""
    n = 0
    while n < max_jobs and run_one(None):
        n += 1
    return n
