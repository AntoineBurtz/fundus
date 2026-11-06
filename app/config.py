"""Zentrale Konfiguration – alles über Umgebungsvariablen steuerbar."""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DATA_DIR = Path(_env("FUNDUS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DB_PATH = DATA_DIR / "fundus.db"
FILES_DIR = DATA_DIR / "files"

# Whisper-Modell für Transkription: tiny, base, small, medium, large-v3, ...
WHISPER_MODEL = _env("FUNDUS_WHISPER_MODEL", "small")
WHISPER_DEVICE = _env("FUNDUS_WHISPER_DEVICE", "auto")  # auto | cpu | cuda
WHISPER_COMPUTE = _env("FUNDUS_WHISPER_COMPUTE", "int8")
# Leer = automatische Spracherkennung, sonst z. B. "de"
WHISPER_LANGUAGE = _env("FUNDUS_WHISPER_LANGUAGE", "") or None

# Videos zusätzlich zum Ton behalten (braucht deutlich mehr Platz)
KEEP_VIDEO = _env("FUNDUS_KEEP_VIDEO", "0") == "1"

# Optionaler Zugangsschutz (HTTP Basic Auth). Leer = kein Login.
AUTH_USER = _env("FUNDUS_USER", "fundus")
AUTH_PASSWORD = _env("FUNDUS_PASSWORD", "")

# Hintergrund-Worker im selben Prozess starten
RUN_WORKER = _env("FUNDUS_RUN_WORKER", "1") == "1"

USER_AGENT = _env(
    "FUNDUS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
