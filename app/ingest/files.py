"""Hochgeladene Dateien: PDF, Text, HTML, Audio/Video, Bilder."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from .. import config, evidence, repo
from ..db import connection
from ..jobs import JobContext
from ..util import normalize_date, sha256_file

TEXT_TYPES = {"text/plain", "text/markdown", "text/csv"}


def kind_for(filename: str, mime: str | None) -> str:
    mime = mime or mimetypes.guess_type(filename)[0] or ""
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    return "document"


def extract_file(path: Path, mime: str | None) -> dict:
    """Text und Metadaten aus einer Datei lesen."""
    mime = mime or mimetypes.guess_type(path.name)[0] or ""
    if mime == "application/pdf" or path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append((page.extract_text() or "").strip())
            except Exception:
                pages.append("")
        meta = reader.metadata or {}
        published = None
        raw_date = meta.get("/CreationDate") if meta else None
        if raw_date and str(raw_date).startswith("D:") and len(str(raw_date)) >= 10:
            try:
                published = normalize_date(str(raw_date)[2:10])
            except ValueError:
                published = None
        return {
            "text": "\n\n".join(p for p in pages if p),
            "title": str(meta.get("/Title") or "") if meta else "",
            "author": str(meta.get("/Author") or "") if meta else "",
            "published_at": published,
        }
    if mime in ("text/html", "application/xhtml+xml") or path.suffix.lower() in (".html", ".htm"):
        from .web import extract_html

        return extract_html(path.read_text(errors="replace"))
    if mime in TEXT_TYPES or mime.startswith("text/") or path.suffix.lower() in (".md", ".txt"):
        return {"text": path.read_text(errors="replace")}
    if path.suffix.lower() == ".docx":
        return {"text": _docx_text(path)}
    return {"text": ""}


def _docx_text(path: Path) -> str:
    import re
    import zipfile

    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    out = []
    for p in paras:
        out.append("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S)))
    import html

    return "\n".join(html.unescape(t) for t in out if t.strip())


def ingest_file(source_id: int, job: JobContext):
    with connection() as conn:
        src = repo.get_source(conn, source_id)
    path = config.FILES_DIR / src["snapshot_path"]
    fields: dict = {"content_hash": sha256_file(path)}
    with connection() as conn:
        vid, _, _ = evidence.create_version(conn, source_id, "erstsicherung", title=src["title"])
        evidence.add_artifact(conn, source_id, vid, "original", path, src["mime_type"])
    seal = ("seal", {"version_id": vid})
    if src["kind"] in ("audio", "video"):
        from .media import probe_duration

        fields["media_path"] = src["snapshot_path"]
        fields["duration"] = probe_duration(path)
        with connection() as conn:
            repo.update_source(conn, source_id, **fields)
        return [seal, ("transcribe", job.options)]
    if src["kind"] == "document":
        info = extract_file(path, src["mime_type"])
        fields["text"] = info.get("text", "")
        for key in ("title", "author", "published_at", "site_name", "description"):
            if info.get(key) and not src[key]:
                fields[key] = info[key]
    with connection() as conn:
        repo.update_source(conn, source_id, **fields)
    return [seal]
