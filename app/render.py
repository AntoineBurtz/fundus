"""Text mit Markierungen in HTML-Absätze umsetzen."""
from __future__ import annotations

from html import escape
from typing import Any, Iterable


def color_slug(color: str) -> str:
    return color.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")


def resolve_highlights(text: str, highlights: Iterable[Any]) -> tuple[list[dict], list[dict]]:
    """Prüft, ob Markierungen noch zum Text passen (z. B. nach erneuter Transkription).

    Gibt (gültige, verwaiste) Markierungen zurück; verschobene werden neu verortet.
    """
    ok, orphans = [], []
    for h in highlights:
        h = dict(h)
        s, e = h["start_offset"], h["end_offset"]
        if text[s:e] == h["quote"]:
            ok.append(h)
            continue
        idx = text.find(h["quote"])
        if idx >= 0 and h["quote"]:
            h["start_offset"], h["end_offset"] = idx, idx + len(h["quote"])
            ok.append(h)
        else:
            orphans.append(h)
    return ok, orphans


def render_blocks(
    text: str, highlights: list[dict], segments: list[Any] | None = None
) -> list[dict]:
    """Zerlegt den Text in Zeilen-Blöcke mit eingebetteten <mark>-Elementen.

    Jeder Block kennt seinen Zeichen-Offset (data-off), damit die Auswahl im
    Browser exakt auf Positionen in sources.text zurückgerechnet werden kann.
    """
    seg_by_off = {s["char_offset"]: s for s in (segments or [])}
    blocks: list[dict] = []
    pos = 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if not line.strip():
            continue
        seg = seg_by_off.get(start)
        blocks.append({
            "offset": start,
            "html": _render_line(line, start, end, highlights),
            "time": seg["start"] if seg else None,
            "speaker": seg["speaker"] if seg else None,
        })
    return blocks


def _render_line(line: str, a: int, b: int, highlights: list[dict]) -> str:
    hits = [h for h in highlights if h["start_offset"] < b and h["end_offset"] > a]
    if not hits:
        return escape(line)
    cuts = {a, b}
    for h in hits:
        cuts.add(max(a, h["start_offset"]))
        cuts.add(min(b, h["end_offset"]))
    points = sorted(cuts)
    out = []
    for x, y in zip(points, points[1:]):
        piece = escape(line[x - a:y - a])
        cover = [h for h in hits if h["start_offset"] <= x and h["end_offset"] >= y]
        if not cover:
            out.append(piece)
            continue
        top = max(cover, key=lambda h: h["id"])
        ids = ",".join(str(h["id"]) for h in cover)
        extra = " hl-multi" if len(cover) > 1 else ""
        out.append(
            f'<mark class="hl c-{color_slug(top["color"])}{extra}" data-ids="{ids}">{piece}</mark>'
        )
    return "".join(out)
