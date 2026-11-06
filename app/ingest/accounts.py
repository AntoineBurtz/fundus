"""Neue Stories, Highlights und Beiträge beobachteter Accounts abholen (gallery-dl).

Jeder neue Inhalt wird eine eigene Quelle mit Fassung, Prüfsumme und Zeitstempel;
Videos werden auf Wunsch transkribiert. Bereits gesicherte Inhalte werden über
ein Download-Archiv und die externe ID nie doppelt geladen.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, evidence, repo, social
from ..db import connection
from ..jobs import JobContext
from ..util import human_datetime, sha256_file

log = logging.getLogger("fundus.accounts")

VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
KIND_LABEL = {"stories": "Story", "highlights": "Highlight", "posts": "Beitrag"}

AUTH_HINTS = ("login", "log in", "logged in", "401", "403", "authentication", "challenge",
              "checkpoint", "redirect", "cookies", "sessionid")
RATE_HINTS = ("429", "too many requests", "rate limit", "please wait", "try again later")


class AuthError(Exception):
    pass


def run_gallery_dl(url: str, dest: Path, cookies: Path, archive: Path,
                   date_min: datetime | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--cookies", str(cookies),
        "--download-archive", str(archive),
        "--write-metadata",
        "--no-mtime",
        "-D", str(dest),
        "-o", "videos=true",
    ]
    if config.SOCIAL_USER_AGENT:
        cmd += ["--user-agent", config.SOCIAL_USER_AGENT]
    if date_min is not None:
        cmd += ["--filter", f"date >= datetime({date_min.year}, {date_min.month}, {date_min.day})"]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=config.SOCIAL_TIMEOUT)


def classify(proc: subprocess.CompletedProcess) -> str | None:
    """None = in Ordnung, sonst 'auth' | 'rate' | 'notfound' | 'error'."""
    if proc.returncode == 0:
        return None
    text = (proc.stderr + proc.stdout).lower()
    if any(h in text for h in RATE_HINTS):
        return "rate"
    if any(h in text for h in AUTH_HINTS):
        return "auth"
    if "not found" in text or "404" in text or "does not exist" in text:
        return "notfound"
    # Exit-Code 4 bei gallery-dl = einzelne Dateien nicht ladbar; Rest ist evtl. trotzdem da
    return "error"


def check_account(source_id, job: JobContext):
    account_id = job.options.get("account_id")
    with connection() as conn:
        acc = social.get_account(conn, account_id)
    if acc is None or not acc["active"]:
        return None
    platform, handle = acc["platform"], acc["handle"]
    label = f"{social.PLATFORMS[platform]} @{handle}"
    cookies = social.cookie_path(platform)
    if not cookies.is_file():
        with connection() as conn:
            social.record_check(conn, account_id, "Keine Cookies hinterlegt",
                                "Unter Einstellungen die Cookies des Recherche-Kontos hochladen.", 0)
        return None

    tmp = config.DATA_DIR / "tmp" / f"account-{account_id}-{int(time.time())}"
    archive = config.DATA_DIR / "social-archive.sqlite3"
    new_items = 0
    problems: list[str] = []
    auth_failed = False
    created = datetime.fromisoformat(acc["created_at"].replace("Z", "+00:00"))
    try:
        for kind, url in social.content_urls(acc):
            job.progress(None, f"{label}: {KIND_LABEL[kind]}s abrufen", force=True)
            dest = tmp / kind
            dest.mkdir(parents=True, exist_ok=True)
            # Beiträge erst ab kurz vor Beobachtungsbeginn – sonst käme das ganze Profil
            date_min = created - timedelta(days=3) if kind == "posts" else None
            try:
                proc = run_gallery_dl(url, dest, cookies, archive, date_min)
            except subprocess.TimeoutExpired:
                problems.append(f"{KIND_LABEL[kind]}: Zeitüberschreitung")
                continue
            problem = classify(proc)
            if problem == "auth":
                auth_failed = True
            if problem and problem != "error":
                problems.append({"auth": "Anmeldung abgelaufen oder blockiert",
                                 "rate": "Zu viele Anfragen – Plattform bremst",
                                 "notfound": "Account nicht gefunden oder privat"}[problem])
            elif problem == "error" and not any(dest.rglob("*.json")):
                last = (proc.stderr.strip().splitlines() or ["unbekannter Fehler"])[-1]
                problems.append(f"{KIND_LABEL[kind]}: {last[:200]}")
            new_items += import_downloads(acc, kind, dest)
            job.check_cancel()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        status = "; ".join(dict.fromkeys(problems))
    else:
        status = f"{new_items} neu" if new_items else "nichts Neues"
    with connection() as conn:
        previous = social.get_account(conn, account_id)["last_status"]
        social.record_check(conn, account_id, status, status if problems else None, new_items)
    if auth_failed and previous != status:
        from .watch import notify

        notify({"id": 0, "title": f"Recherche-Konto {social.PLATFORMS[platform]}", "url": ""},
               "Anmeldung abgelaufen – bitte neue Cookies hochladen")
    return None


def _meta_id(meta: dict) -> str | None:
    for key in ("media_id", "id", "post_id", "pk"):
        if meta.get(key):
            return str(meta[key])
    return None


def _parse_date(meta: dict) -> datetime | None:
    raw = meta.get("date") or meta.get("post_date")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, timezone.utc)
    if isinstance(raw, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _item_url(platform: str, handle: str, kind: str, meta: dict, mid: str) -> str:
    if meta.get("post_url"):
        return meta["post_url"]
    if platform == "instagram":
        if kind == "stories":
            return f"https://www.instagram.com/stories/{handle}/{meta.get('media_id') or mid}/"
        if meta.get("post_shortcode") or meta.get("shortcode"):
            return f"https://www.instagram.com/p/{meta.get('post_shortcode') or meta.get('shortcode')}/"
        return social.profile_url(platform, handle)
    return f"https://www.tiktok.com/@{handle}/video/{meta.get('id') or mid}"


def import_downloads(acc, kind: str, folder: Path) -> int:
    """Jede heruntergeladene Datei (mit Metadaten-JSON daneben) als Quelle anlegen."""
    platform, handle = acc["platform"], acc["handle"]
    n = 0
    for meta_path in sorted(folder.rglob("*.json")):
        media = meta_path.with_suffix("")  # foo.mp4.json → foo.mp4
        if not media.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        mid = _meta_id(meta) or media.stem
        external_id = f"{platform}:{mid}:{meta.get('num') or 1}"
        when = _parse_date(meta)
        is_video = media.suffix.lower() in VIDEO_EXT
        art = KIND_LABEL[kind]
        if meta.get("highlight_title"):
            art += f" „{meta['highlight_title']}“"
        title = f"{social.PLATFORMS[platform]}-{art} von @{handle}"
        if when:
            title += f", {human_datetime(when.strftime('%Y-%m-%dT%H:%M:%SZ'))}"
        caption = meta.get("description") or meta.get("desc") or meta.get("caption") or ""
        url = _item_url(platform, handle, kind, meta, mid)
        with connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sources WHERE project_id = ? AND external_id = ?",
                (acc["project_id"], external_id)).fetchone()
            if exists:
                continue
            sid = repo.create_source(
                conn, acc["project_id"], "video" if is_video else "image",
                title=title, url=url, author=f"@{handle}", site_name=social.PLATFORMS[platform],
                description=caption, published_at=when.strftime("%Y-%m-%d") if when else None,
                event_date=when.strftime("%Y-%m-%d") if when else None,
                external_id=external_id, account_id=acc["id"],
            )
            repo.set_source_tags(conn, sid, [KIND_LABEL[kind], f"@{handle}"])
            vid, _, vfolder = evidence.create_version(conn, sid, "erstsicherung", final_url=url, title=title)
            target = vfolder / f"{kind}{media.suffix.lower()}"
            shutil.move(str(media), target)
            meta_target = vfolder / "metadaten.json"
            shutil.move(str(meta_path), meta_target)
            mime = mimetypes.guess_type(target.name)[0]
            evidence.add_artifact(conn, sid, vid, "media" if is_video else "original", target, mime)
            evidence.add_artifact(conn, sid, vid, "metadata", meta_target, "application/json")
            rel = str(target.relative_to(config.FILES_DIR))
            repo.update_source(conn, sid, snapshot_path=rel, media_path=rel if is_video else None,
                               mime_type=mime, content_hash=sha256_file(target),
                               duration=meta.get("duration") or meta.get("video_duration"))
            repo.enqueue_job(conn, sid, "seal", {"version_id": vid})
            if is_video and acc["transcribe"]:
                repo.enqueue_job(conn, sid, "transcribe", {"subtitles": "off"})
            n += 1
    return n
