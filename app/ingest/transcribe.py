"""Lokale Transkription mit faster-whisper (Segmente mit Zeitstempeln)."""
from __future__ import annotations

import threading

from .. import config, repo
from ..db import connection

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "faster-whisper ist nicht installiert (pip install faster-whisper)"
                ) from e
            _model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE,
                download_root=str(config.DATA_DIR / "models"),
            )
        return _model


def transcribe(source_id: int) -> str | None:
    with connection() as conn:
        src = repo.get_source(conn, source_id)
    if not src["media_path"]:
        raise RuntimeError("Keine Mediendatei zum Transkribieren vorhanden")
    path = config.FILES_DIR / src["media_path"]
    model = _get_model()
    segments_iter, info = model.transcribe(
        str(path),
        language=config.WHISPER_LANGUAGE,
        vad_filter=True,
        beam_size=5,
    )
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_iter]
    with connection() as conn:
        repo.set_transcript(conn, source_id, segments, language=info.language or "")
        if not src["duration"] and getattr(info, "duration", None):
            repo.update_source(conn, source_id, duration=info.duration)
    return None
