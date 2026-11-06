"""Unterschiede zwischen zwei Fassungen: absatzweise, innerhalb geänderter Absätze wortweise."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from html import escape

_WORD = re.compile(r"\S+|\s+")


def _paras(text: str) -> list[str]:
    return [" ".join(p.split()) for p in (text or "").splitlines() if p.strip()]


def _word_diff(a: str, b: str) -> str:
    wa, wb = _WORD.findall(a), _WORD.findall(b)
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(escape("".join(wa[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{escape(''.join(wa[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{escape(''.join(wb[j1:j2]))}</ins>")
    return "".join(out)


def diff_blocks(old: str, new: str, context: int = 1) -> tuple[list[dict], dict]:
    """Liefert Anzeige-Blöcke und eine Zusammenfassung (Anzahl geänderter Absätze)."""
    a, b = _paras(old), _paras(new)
    raw: list[dict] = []
    stats = {"added": 0, "removed": 0, "changed": 0}
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            raw += [{"type": "same", "html": escape(p)} for p in a[i1:i2]]
        elif op == "delete":
            raw += [{"type": "del", "html": escape(p)} for p in a[i1:i2]]
            stats["removed"] += i2 - i1
        elif op == "insert":
            raw += [{"type": "ins", "html": escape(p)} for p in b[j1:j2]]
            stats["added"] += j2 - j1
        else:
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                raw.append({"type": "change", "html": _word_diff(a[i1 + k], b[j1 + k])})
            stats["changed"] += pairs
            raw += [{"type": "del", "html": escape(p)} for p in a[i1 + pairs:i2]]
            raw += [{"type": "ins", "html": escape(p)} for p in b[j1 + pairs:j2]]
            stats["removed"] += (i2 - i1) - pairs
            stats["added"] += (j2 - j1) - pairs
    # Lange unveränderte Strecken einklappen
    keep = [False] * len(raw)
    for i, blk in enumerate(raw):
        if blk["type"] != "same":
            for k in range(max(0, i - context), min(len(raw), i + context + 1)):
                keep[k] = True
    out: list[dict] = []
    skipped = 0
    for blk, k in zip(raw, keep):
        if k:
            if skipped:
                out.append({"type": "skip", "count": skipped})
                skipped = 0
            out.append(blk)
        else:
            skipped += 1
    if skipped:
        out.append({"type": "skip", "count": skipped})
    return out, stats
