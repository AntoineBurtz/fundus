"""Untertitel auswählen und in Transkript-Segmente umwandeln (VTT, SRT, TTML)."""
from __future__ import annotations

import html
import re
from typing import Any

from .. import config

TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
TAG_RE = re.compile(r"<[^>]+>")
SUPPORTED_EXT = ("vtt", "srt", "ttml", "dfxp", "xml")


# ------------------------------------------------------------- Auswahl

def _lang_match(key: str, wanted: str) -> bool:
    k = key.lower()
    w = wanted.lower()
    return k == w or k.startswith(w + "-") or k.startswith(w + "_")


def _best_format(formats: list[dict]) -> dict | None:
    by_ext = {f.get("ext"): f for f in formats if f.get("url")}
    for ext in SUPPORTED_EXT:
        if ext in by_ext:
            return by_ext[ext]
    return None


def pick_subtitles(info: dict[str, Any], mode: str | None = None,
                   langs: list[str] | None = None) -> dict | None:
    """Wählt die beste Untertitelspur aus den yt-dlp-Metadaten.

    Reihenfolge: handgemachte Untertitel in bevorzugter Sprache → handgemachte in
    Originalsprache → einzige vorhandene handgemachte Spur → (bei mode=auto)
    automatische Untertitel in Originalsprache. Automatisch *übersetzte* Spuren
    werden nie genommen.
    """
    mode = mode or config.SUBTITLES
    if mode == "off":
        return None
    langs = langs or config.SUBTITLE_LANGS
    original = (info.get("language") or "").lower() or None

    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    candidates: list[str] = []
    for want in [*langs, *([original] if original else [])]:
        candidates += [k for k in manual if _lang_match(k, want) and k not in candidates]
    if not candidates and len(manual) == 1:
        candidates = list(manual)
    for key in candidates:
        fmt = _best_format(manual[key])
        if fmt:
            return {"lang": key, "auto": False, "url": fmt["url"], "ext": fmt.get("ext")}

    if mode != "auto":
        return None
    auto = info.get("automatic_captions") or {}
    # YouTube markiert die Originalspur mit "-orig"; sonst die Originalsprache selbst
    keys = [k for k in auto if k.endswith("-orig")]
    if original:
        keys += [k for k in auto if k.lower() == original and k not in keys]
    for key in keys:
        fmt = _best_format(auto[key])
        if fmt:
            return {"lang": key.removesuffix("-orig"), "auto": True, "url": fmt["url"],
                    "ext": fmt.get("ext")}
    return None


# ------------------------------------------------------------- Parser

def _ts(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _clean(line: str) -> str:
    line = TAG_RE.sub("", line)
    line = html.unescape(line).replace("​", "")
    return " ".join(line.split())


def parse_vtt_srt(text: str) -> list[dict]:
    """Liest WebVTT und SRT. Entfernt die bei YouTube üblichen Wiederholungen
    ("Roll-up"-Untertitel, bei denen jede Zeile zweimal erscheint)."""
    cues: list[dict] = []
    prev_lines: list[str] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("﻿", ""))
    for block in blocks:
        lines = block.strip().splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = TIME_RE.search(lines[idx])
        if not m:
            continue
        start = _ts(*m.groups()[:4])
        end = _ts(*m.groups()[4:])
        texts = [t for t in (_clean(l) for l in lines[idx + 1:]) if t]
        if not texts:
            prev_lines = []
            continue
        if end - start < 0.05:  # YouTube-Übergangs-Cues
            prev_lines = texts
            continue
        new = [t for t in texts if t not in prev_lines]
        prev_lines = texts
        if new:
            cues.append({"start": start, "end": end, "text": " ".join(new)})
    return cues


def _ttml_time(value: str | None, tick_rate: float) -> float | None:
    if not value:
        return None
    v = value.strip()
    if m := re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:[.:](\d+))?", v):
        h, mi, s, frac = m.groups()
        return int(h) * 3600 + int(mi) * 60 + int(s) + (float("0." + frac) if frac else 0)
    if m := re.fullmatch(r"([\d.]+)(h|m|s|ms|t)", v):
        num, unit = float(m.group(1)), m.group(2)
        return num * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}.get(unit, 1 / tick_rate)
    return None


def parse_ttml(text: str) -> list[dict]:
    rate_m = re.search(r'tickRate="(\d+)"', text)
    tick_rate = float(rate_m.group(1)) if rate_m else 10_000_000.0
    cues = []
    for m in re.finditer(r"<(?:\w+:)?p\b([^>]*)>(.*?)</(?:\w+:)?p>", text, flags=re.S):
        attrs, body = m.groups()
        start = _ttml_time((re.search(r'\bbegin="([^"]+)"', attrs) or [None, None])[1], tick_rate)
        end = _ttml_time((re.search(r'\bend="([^"]+)"', attrs) or [None, None])[1], tick_rate)
        if start is None or end is None:
            continue
        body = re.sub(r"<(?:\w+:)?br\s*/?>", " ", body)
        t = _clean(body)
        if t:
            cues.append({"start": start, "end": end, "text": t})
    return cues


def parse_subtitles(text: str, ext: str | None = None) -> list[dict]:
    ext = (ext or "").lower()
    if ext in ("ttml", "dfxp", "xml") or text.lstrip().startswith("<"):
        return merge_cues(parse_ttml(text))
    return merge_cues(parse_vtt_srt(text))


def merge_cues(cues: list[dict], max_chars: int = 240, max_secs: float = 20.0,
               min_chars: int = 60) -> list[dict]:
    """Fasst kurze Untertitel-Schnipsel zu lesbaren Absätzen zusammen –
    bevorzugt an Satzenden."""
    out: list[dict] = []
    cur: dict | None = None
    for c in sorted(cues, key=lambda c: c["start"]):
        if cur is None:
            cur = dict(c)
        else:
            cur["text"] = f"{cur['text']} {c['text']}"
            cur["end"] = max(cur["end"], c["end"])
        sentence_end = cur["text"].rstrip().endswith((".", "?", "!", "…", '."', '?"'))
        too_long = len(cur["text"]) >= max_chars or cur["end"] - cur["start"] >= max_secs
        if (sentence_end and len(cur["text"]) >= min_chars) or too_long:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out
