"""Einfacher Hintergrund-Worker: arbeitet die Job-Tabelle nacheinander ab."""
from __future__ import annotations

import logging
import threading
import time
import traceback

from . import repo
from .db import connection
from .ingest.files import ingest_file
from .ingest.media import ingest_media
from .ingest.transcribe import transcribe
from .ingest.web import ingest_web

log = logging.getLogger("fundus.worker")

HANDLERS = {
    "web": ingest_web,
    "media": ingest_media,
    "file": ingest_file,
    "transcribe": transcribe,
}

_stop = threading.Event()
_wake = threading.Event()


def run_one() -> bool:
    """Bearbeitet einen Job. Gibt False zurück, wenn keiner anstand."""
    with connection() as conn:
        job = repo.claim_next_job(conn)
    if job is None:
        return False
    handler = HANDLERS.get(job["type"])
    error = None
    follow_up = None
    try:
        if handler is None:
            raise RuntimeError(f"Unbekannter Jobtyp: {job['type']}")
        log.info("Job %s (%s) für Quelle %s", job["id"], job["type"], job["source_id"])
        follow_up = handler(job["source_id"])
    except Exception as e:  # Fehler am Job speichern, Worker läuft weiter
        log.warning("Job %s fehlgeschlagen: %s", job["id"], e)
        log.debug(traceback.format_exc())
        error = f"{type(e).__name__}: {e}"[:2000]
    with connection() as conn:
        if follow_up and not error:
            repo.enqueue_job(conn, job["source_id"], follow_up)
        repo.finish_job(conn, job["id"], error)
    return True


def _loop() -> None:
    with connection() as conn:
        repo.requeue_stale_jobs(conn)
    while not _stop.is_set():
        try:
            worked = run_one()
        except Exception:  # pragma: no cover
            log.exception("Worker-Fehler")
            worked = False
        if not worked:
            _wake.wait(3)
            _wake.clear()


def wake() -> None:
    _wake.set()


def start() -> threading.Thread:
    t = threading.Thread(target=_loop, name="fundus-worker", daemon=True)
    t.start()
    return t


def stop() -> None:
    _stop.set()
    _wake.set()


def drain(max_jobs: int = 100) -> int:
    """Alle anstehenden Jobs synchron abarbeiten (für Tests und CLI)."""
    n = 0
    while n < max_jobs and run_one():
        n += 1
    return n
