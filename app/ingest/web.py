"""Webseiten sichern: Original-HTML + lesbarer Text + Metadaten."""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import urlparse

import httpx
import trafilatura

from .. import config, repo
from ..db import connection
from ..jobs import JobContext
from ..util import sha256_bytes

# Domains, deren Links standardmäßig als Video/Audio geladen werden
MEDIA_DOMAINS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "soundcloud.com",
    "mixcloud.com", "ardaudiothek.de", "ardmediathek.de", "zdf.de", "arte.tv",
    "podcasts.apple.com", "podigee.io", "podcast.de", "twitch.tv", "tiktok.com",
    "rumble.com", "br.de/mediathek", "3sat.de", "media.ccc.de", "peertube",
)
MEDIA_EXT = re.compile(r"\.(mp3|m4a|aac|ogg|opus|wav|flac|mp4|m4v|webm|mov|mkv)(\?|$)", re.I)


def looks_like_media(url: str) -> bool:
    if MEDIA_EXT.search(urlparse(url).path or ""):
        return True
    host_path = (urlparse(url).netloc + urlparse(url).path).lower()
    return any(d in host_path for d in MEDIA_DOMAINS)


def extract_html(html: str, url: str | None = None) -> dict:
    """Extrahiert Haupttext und Metadaten aus HTML."""
    doc = trafilatura.bare_extraction(
        html, url=url, with_metadata=True, include_comments=False,
        include_tables=True, favor_recall=True,
    )
    data = doc.as_dict() if doc is not None and hasattr(doc, "as_dict") else (doc or {})
    text = (data.get("text") or "").strip()
    title = data.get("title") or ""
    # Doppelte Überschrift am Textanfang entfernen
    if title and text.startswith(title):
        text = text[len(title):].lstrip("\n ")
    return {
        "title": title,
        "author": data.get("author") or "",
        "site_name": data.get("sitename") or "",
        "description": data.get("description") or "",
        "published_at": data.get("date") or None,
        "language": data.get("language") or "",
        "text": text,
    }


def ingest_web(source_id: int, job: JobContext):
    """Ruft die Seite ab. Gibt ggf. einen Folge-Jobtyp zurück (z. B. bei Mediendateien)."""
    with connection() as conn:
        src = repo.get_source(conn, source_id)
    url = src["url"]
    job.progress(None, "Seite abrufen", force=True)
    resp = httpx.get(
        url, follow_redirects=True, timeout=45,
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "de,en;q=0.8"},
    )
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()

    if ctype.startswith(("audio/", "video/")):
        with connection() as conn:
            repo.update_source(conn, source_id, kind="audio" if ctype.startswith("audio/") else "video")
        return ("media", job.options)

    folder = repo.source_dir(source_id)
    folder.mkdir(parents=True, exist_ok=True)
    body = resp.content

    if ctype == "application/pdf" or url.lower().endswith(".pdf"):
        path = folder / "original.pdf"
        path.write_bytes(body)
        from .files import extract_file  # lazy, vermeidet Zyklus

        info = extract_file(path, "application/pdf")
        with connection() as conn:
            repo.update_source(
                conn, source_id, kind="document", snapshot_path=str(path.relative_to(config.FILES_DIR)),
                mime_type="application/pdf", content_hash=sha256_bytes(body),
                title=src["title"] or info.get("title") or urlparse(url).path.rsplit("/", 1)[-1],
                author=src["author"] or info.get("author", ""), text=info.get("text", ""),
            )
        return None

    path = folder / "page.html"
    path.write_bytes(body)
    html = resp.text
    meta = extract_html(html, str(resp.url))
    fields = {
        "snapshot_path": str(path.relative_to(config.FILES_DIR)),
        "mime_type": ctype or mimetypes.guess_type(url)[0],
        "content_hash": sha256_bytes(body),
        "text": meta["text"],
    }
    # Vom Nutzer gesetzte Werte nicht überschreiben
    for key in ("title", "author", "site_name", "description", "published_at", "language"):
        if meta.get(key) and not src[key]:
            fields[key] = meta[key]
    if not (fields.get("title") or src["title"]):
        fields["title"] = urlparse(url).netloc
    if str(resp.url) != url:
        fields["notes"] = ((src["notes"] + "\n") if src["notes"] else "") + f"Weitergeleitet zu: {resp.url}"
    with connection() as conn:
        repo.update_source(conn, source_id, **fields)
    return None
