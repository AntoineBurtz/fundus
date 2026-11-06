"""Kontext für laufende Jobs: Optionen, Fortschritt, Restzeit, Abbruch."""
from __future__ import annotations

import json
import time
from typing import Any

from . import repo
from .db import connection


# Mindestabstand zwischen zwei Fortschritts-Schreibvorgängen (Sekunden)
DEFAULT_INTERVAL = 2.0


class Cancelled(Exception):
    """Der Job wurde vom Nutzer abgebrochen."""


class JobContext:
    """Wird an jeden Job-Handler übergeben.

    progress() schreibt höchstens alle `interval` Sekunden in die Datenbank,
    berechnet die Restzeit je Arbeitsschritt und prüft dabei auf Abbruch.
    """

    def __init__(self, job_id: int | None, source_id: int, options: dict[str, Any] | None = None,
                 interval: float | None = None):
        self.id = job_id
        self.source_id = source_id
        self.options = options or {}
        self.interval = DEFAULT_INTERVAL if interval is None else interval
        self._last_write = 0.0
        self._phase: str | None = None
        self._phase_start = time.monotonic()

    @classmethod
    def from_row(cls, row) -> "JobContext":
        try:
            opts = json.loads(row["options"] or "{}")
        except (ValueError, TypeError):
            opts = {}
        return cls(row["id"], row["source_id"], opts)

    def progress(self, fraction: float | None, note: str, force: bool = False) -> None:
        now = time.monotonic()
        if note != self._phase:
            self._phase, self._phase_start = note, now
            force = True
        if not force and now - self._last_write < self.interval:
            return
        self._last_write = now
        eta = None
        if fraction is not None:
            fraction = max(0.0, min(1.0, fraction))
            elapsed = now - self._phase_start
            if fraction >= 0.02 and elapsed > 3:
                eta = elapsed * (1 - fraction) / fraction
        if self.id is None:
            return
        with connection() as conn:
            repo.set_job_progress(conn, self.id, fraction, eta, note)
            if repo.job_cancel_requested(conn, self.id):
                raise Cancelled()

    def check_cancel(self) -> None:
        if self.id is None:
            return
        with connection() as conn:
            if repo.job_cancel_requested(conn, self.id):
                raise Cancelled()
