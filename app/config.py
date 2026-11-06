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

# CPU-Threads für Whisper. 0 = automatisch (halbe Kernzahl, damit der Server
# nebenher reaktionsfähig bleibt)
_threads = int(_env("FUNDUS_WHISPER_THREADS", "0") or 0)
WHISPER_THREADS = _threads if _threads > 0 else max(1, (os.cpu_count() or 2) // 2)

# Auswahl in der Oberfläche: Modell → Beschreibung
WHISPER_MODELS = {
    "tiny": "sehr schnell, ungenau",
    "base": "schnell, brauchbar",
    "small": "ausgewogen",
    "medium": "genau, langsam",
    "large-v3-turbo": "am genauesten, sehr langsam auf CPU",
}

# Vorhandene Untertitel statt Whisper nutzen:
#   auto   = handgemachte bevorzugt, automatische (Originalsprache) akzeptiert
#   manual = nur handgemachte Untertitel
#   off    = immer Whisper
SUBTITLES = _env("FUNDUS_SUBTITLES", "auto")
# Bevorzugte Untertitelsprachen in Reihenfolge
SUBTITLE_LANGS = [x.strip() for x in _env("FUNDUS_SUBTITLE_LANGS", "de,en").split(",") if x.strip()]

# ---- Beweissicherung
# Screenshot + PDF + gerenderte Seite per Chromium (Playwright)
CAPTURE = _env("FUNDUS_CAPTURE", "1") == "1"
CHROMIUM_PATH = _env("FUNDUS_CHROMIUM_PATH", "") or None  # leer = Playwright-Chromium
CAPTURE_ACCEPT_COOKIES = _env("FUNDUS_CAPTURE_ACCEPT_COOKIES", "1") == "1"
# Unabhängige Zeitstempel nach RFC 3161. Es wird nur eine Prüfsumme übertragen.
TSA_URLS = [u.strip() for u in _env(
    "FUNDUS_TSA_URLS", "http://timestamp.digicert.com,https://freetsa.org/tsr"
).split(",") if u.strip()]
# Benachrichtigung bei Änderungen (ntfy-Topic-URL oder beliebiger JSON-Webhook,
# z. B. Home-Assistant-Webhook). Leer = aus.
NOTIFY_URL = _env("FUNDUS_NOTIFY_URL", "")
# Öffentliche Adresse von Fundus für Links in Benachrichtigungen
BASE_URL = _env("FUNDUS_BASE_URL", "").rstrip("/")

# ---- Social-Media-Beobachtung (gallery-dl)
# Optional: User-Agent des Browsers, aus dem die Cookies stammen (verringert Rückfragen)
SOCIAL_USER_AGENT = _env("FUNDUS_SOCIAL_USER_AGENT", "")
SOCIAL_TIMEOUT = int(_env("FUNDUS_SOCIAL_TIMEOUT", "1200"))

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
