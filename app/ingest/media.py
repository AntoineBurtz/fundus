"""Videos und Podcasts herunterladen (yt-dlp), Metadaten und Untertitel übernehmen."""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from .. import config, repo
from ..db import connection
from ..jobs import JobContext
from ..util import normalize_date, sha256_file
from .subtitles import parse_subtitles, pick_subtitles

log = logging.getLogger("fundus.media")

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".weba"}


def _progress_hook(job: JobContext):
    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        job.progress(done / total if total else None, "Download")
    return hook


def ingest_media(source_id: int, job: JobContext):
    import yt_dlp

    with connection() as conn:
        src = repo.get_source(conn, source_id)
    folder = repo.source_dir(source_id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("media.*"):  # bei Wiederholung alte Teil-Downloads entfernen
        old.unlink(missing_ok=True)

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
        "progress_hooks": [_progress_hook(job)],
    }
    if config.KEEP_VIDEO:
        opts["merge_output_format"] = "mp4"

    job.progress(None, "Metadaten abrufen", force=True)
    subtitle_segments: list[dict] = []
    subtitle_origin = None
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(src["url"], download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next(e for e in info["entries"] if e)

        # Untertitel zuerst – spart auf CPU-Servern viel Zeit
        sub_mode = "off" if job.options.get("force_whisper") else job.options.get("subtitles")
        track = pick_subtitles(info, sub_mode)
        if track:
            try:
                raw = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
                subtitle_segments = parse_subtitles(raw, track["ext"])
                kind = "automatische Untertitel" if track["auto"] else "Untertitel"
                subtitle_origin = f"{kind} ({track['lang']})"
            except Exception as e:  # Untertitel sind optional
                log.warning("Untertitel für Quelle %s nicht ladbar: %s", source_id, e)

        job.progress(0.0, "Download", force=True)
        ydl.process_ie_result(info, download=True)
        files = sorted(folder.glob("media.*"), key=lambda p: p.stat().st_size, reverse=True)
        files = [f for f in files if not f.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Download lieferte keine Datei")
        media = files[0]

    has_video = bool(info.get("vcodec") and info.get("vcodec") != "none") and media.suffix not in AUDIO_EXT
    # Art nach dem Original bestimmen (die Vorauswahl beim Anlegen war nur geraten):
    # ein YouTube-Video bleibt "Video", auch wenn nur die Tonspur geladen wird.
    has_video_stream = bool(info.get("width")) or any(
        f.get("vcodec") not in (None, "none") for f in info.get("formats") or []
    )
    kind = "video" if has_video_stream else "audio"

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
        raw_date = info.get("upload_date") or info.get("release_date")
        if raw_date:
            try:
                fields["published_at"] = normalize_date(raw_date)
            except ValueError:
                pass
    with connection() as conn:
        repo.update_source(conn, source_id, **fields)
        if _usable(subtitle_segments):
            lang = subtitle_origin.rsplit("(", 1)[-1].rstrip(")") if subtitle_origin else ""
            repo.set_transcript(conn, source_id, subtitle_segments,
                                language=lang.split("-")[0], origin=subtitle_origin)
            return None
    return ("transcribe", job.options)


def _usable(segments: list[dict]) -> bool:
    return bool(segments) and sum(len(s["text"]) for s in segments) >= 80


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
