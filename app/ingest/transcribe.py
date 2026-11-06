"""Lokale Transkription mit faster-whisper – auf CPU optimiert, mit Fortschritt."""
from __future__ import annotations

import gc
import threading

from .. import config, repo
from ..db import connection
from ..jobs import JobContext

_model = None
_model_name: str | None = None
_model_lock = threading.Lock()


def _get_model(name: str):
    """Lädt das Modell (beim ersten Mal inkl. Download) und hält nur eines im RAM."""
    global _model, _model_name
    with _model_lock:
        if _model is not None and _model_name == name:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "faster-whisper ist nicht installiert (pip install faster-whisper)"
            ) from e
        _model = None
        gc.collect()
        _model = WhisperModel(
            name,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE,
            cpu_threads=config.WHISPER_THREADS,
            download_root=str(config.DATA_DIR / "models"),
        )
        _model_name = name
        return _model


def model_for(options: dict) -> str:
    name = options.get("whisper_model") or config.WHISPER_MODEL
    return name if name in config.WHISPER_MODELS or name == config.WHISPER_MODEL else config.WHISPER_MODEL


def transcribe(source_id: int, job: JobContext):
    with connection() as conn:
        src = repo.get_source(conn, source_id)
    if not src["media_path"]:
        raise RuntimeError("Keine Mediendatei zum Transkribieren vorhanden")
    path = config.FILES_DIR / src["media_path"]
    name = model_for(job.options)

    job.progress(None, f"Lade Whisper-Modell „{name}“", force=True)
    model = _get_model(name)
    job.check_cancel()

    job.progress(0.0, f"Transkription mit „{name}“", force=True)
    segments_iter, info = model.transcribe(
        str(path),
        language=job.options.get("language") or config.WHISPER_LANGUAGE,
        vad_filter=True,
        beam_size=5,
    )
    total = info.duration or src["duration"] or 0
    segments = []
    for s in segments_iter:  # Generator: Whisper rechnet erst beim Durchlaufen
        segments.append({"start": s.start, "end": s.end, "text": s.text})
        job.progress(s.end / total if total else None, f"Transkription mit „{name}“")

    with connection() as conn:
        repo.set_transcript(conn, source_id, segments, language=info.language or "",
                            origin=f"Whisper {name}")
        if not src["duration"] and total:
            repo.update_source(conn, source_id, duration=total)
    return None
