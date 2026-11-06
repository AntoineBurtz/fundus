"""Kleine Hilfsfunktionen: Datumsangaben, Zeitformat, Hashes."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

_DE_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")
_DE_MONTH = re.compile(r"^\s*(\d{1,2})\.(\d{4})\s*$")
_ISO = re.compile(r"^\s*(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")
_COMPACT = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})\s*$")  # yt-dlp: 20240131

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def normalize_date(value: str | None) -> str | None:
    """Wandelt Datumsangaben in (ggf. unvollständiges) ISO-Format.

    Unvollständige Angaben sind gewollt: "2019" oder "2019-03" sind
    gültige Einordnungen, wenn der genaue Tag unbekannt ist.
    Akzeptiert: 2019, 2019-03, 2019-03-12, 12.03.2019, 03.2019, 20190312,
    sowie ISO-Zeitstempel (Uhrzeit wird verworfen).
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if m := _DE_DATE.match(v):
        d, mo, y = (int(x) for x in m.groups())
        return _fmt(y, mo, d)
    if m := _DE_MONTH.match(v):
        mo, y = (int(x) for x in m.groups())
        return _fmt(y, mo, None)
    if m := _COMPACT.match(v):
        y, mo, d = (int(x) for x in m.groups())
        return _fmt(y, mo, d)
    if m := _ISO.match(v):
        y = int(m.group(1))
        mo = int(m.group(2)) if m.group(2) else None
        d = int(m.group(3)) if m.group(3) else None
        return _fmt(y, mo, d)
    raise ValueError(f"Unbekanntes Datumsformat: {value!r}")


def _fmt(y: int, mo: int | None, d: int | None) -> str:
    if mo is None:
        return f"{y:04d}"
    if not 1 <= mo <= 12:
        raise ValueError(f"Ungültiger Monat: {mo}")
    if d is None:
        return f"{y:04d}-{mo:02d}"
    datetime(y, mo, d)  # validiert den Tag
    return f"{y:04d}-{mo:02d}-{d:02d}"


def human_date(value: str | None) -> str:
    """ISO (auch unvollständig) → deutsche Anzeige."""
    if not value:
        return ""
    parts = value[:10].split("-")
    try:
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return f"{MONTHS_DE[int(parts[1]) - 1]} {parts[0]}"
        return f"{int(parts[2])}. {MONTHS_DE[int(parts[1]) - 1]} {parts[0]}"
    except (ValueError, IndexError):
        return value


def human_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%d.%m.%Y, %H:%M")


def timecode(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FTS_TOKEN = re.compile(r'"([^"]+)"|(\S+)')


def fts_query(user_query: str) -> str:
    """Übersetzt eine Benutzereingabe in eine sichere FTS5-Abfrage.

    - Wörter werden als Präfix gesucht (recherch → Recherche, recherchiert)
    - "Phrasen in Anführungszeichen" bleiben zusammen
    - OR zwischen Begriffen wird unterstützt, sonst gilt UND
    - -wort schließt aus
    """
    parts: list[str] = []
    for phrase, word in _FTS_TOKEN.findall(user_query or ""):
        if phrase:
            parts.append('"' + phrase.replace('"', "") + '"')
            continue
        if word.upper() == "OR" and parts and parts[-1] != "OR":
            parts.append("OR")
            continue
        neg = word.startswith("-") and len(word) > 1
        w = word[1:] if neg else word
        w = re.sub(r"[^\w\-.]", "", w, flags=re.UNICODE).strip(".-")
        if not w:
            continue
        term = '"' + w + '"*'
        parts.append(("NOT " + term) if neg else term)
    while parts and parts[-1] == "OR":
        parts.pop()
    # FTS5 erlaubt kein führendes NOT
    while parts and parts[0].startswith("NOT "):
        parts.pop(0)
    return " ".join(parts)
