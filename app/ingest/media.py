"""Videos und Podcasts herunterladen (yt-dlp) und Metadaten übernehmen."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from .. import config, repo
from ..db import connection
from ..util import normalize_date, sha256_file

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".weba"}


def ingest_media(source_id: int) -> str | None:
    import yt_dlp

    with connection() as conn:
        src = repo.get_source(conn, source_id)
    folder = repo.source_dir(source_id)
    folder.mkdir(parents=True, exist_ok=True)

    fmt = (
        "bv*[height<=720]+ba/b[height<=720]/b" if config.KEEP_VIDEO
        else "ba[ext=m4a]/ba[ext=mp3]/ba/b"
    )
    opts = {
        "format": fmt,
        "outtmpl": str(folder / "media.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "writethumbnail": False,
        "http_headers": {"User-Agent": config.USER_AGENT},
    }
    if config.KEEP_VIDEO:
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(src["url"], download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)
        files = sorted(folder.glob("media.*"), key=lambda p: p.stat().st_size, reverse=True)
        if not files:
            raise RuntimeError("Download lieferte keine Datei")
        media = files[0]

    has_video = bool(info.get("vcodec") and info.get("vcodec") != "none") and media.suffix not in AUDIO_EXT
    kind = src["kind"]
    if kind not in ("video", "audio"):
        kind = "video" if (info.get("vcodec") not in (None, "none") or info.get("width")) else "audio"

    fields = {
        "kind": kind,
        "media_path": str(media.relative_to(config.FILES_DIR)),
        "snapshot_path": str(media.relative_to(config.FILES_DIR)),
        "mime_type": mimetypes.guess_type(media.name)[0] or ("video/mp4" if has_video else "audio/mpeg"),
        "content_hash": sha256_file(media),
        "duration": info.get("duration"),
    }
    candidates = {
        "title": info.get("title") or info.get("fulltitle"),
        "author": info.get("uploader") or info.get("channel") or info.get("artist") or "",
        "site_name": info.get("extractor_key") if info.get("extractor_key") not in (None, "Generic")
                     else (info.get("webpage_url_domain") or ""),
        "description": info.get("description") or "",
        "language": info.get("language") or "",
    }
    for key, val in candidates.items():
        if val and not src[key]:
            fields[key] = val
    if not src["published_at"]:
        raw = info.get("upload_date") or info.get("release_date")
        if raw:
            try:
                fields["published_at"] = normalize_date(raw)
            except ValueError:
                pass
    with connection() as conn:
        repo.update_source(conn, source_id, **fields)
    return "transcribe"


def probe_duration(path: Path) -> float | None:
    """Dauer per ffprobe ermitteln (für hochgeladene Dateien)."""
    import json
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return None
