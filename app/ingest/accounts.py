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


def date_filter(date_min: datetime) -> str:
    """gallery-dl-Filter: nur Inhalte ab date_min, danach Abruf beenden.

    Profile liefern neueste Beiträge zuerst. Sobald ein älterer Beitrag kommt,
    stoppt abort() das Weiterblättern – das spart viele Anfragen. Angeheftete
    (ältere) Beiträge stehen oben und werden übersprungen, ohne abzubrechen.
    """
    d = date_min
    return (f"date >= datetime({d.year}, {d.month}, {d.day}) or "
            "(False if locals().get('pinned') else abort())")


def run_gallery_dl(url: str, dest: Path, cookies: Path, archive: Path,
                   date_min: datetime | None = None, job: JobContext | None = None,
                   note: str = "", timeout: float | None = None) -> subprocess.CompletedProcess:
    """gallery-dl als eigenen Prozess starten; Fortschritt melden, Abbruch ermöglichen."""
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
        cmd += ["--filter", date_filter(date_min)]
    cmd.append(url)
    timeout = timeout or config.SOCIAL_TIMEOUT
    out_path, err_path = dest / ".stdout.log", dest / ".stderr.log"
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True)
        started = time.monotonic()
        try:
            while True:
                try:
                    proc.wait(timeout=3)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() - started > timeout:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if job is not None:
                    n = sum(1 for _ in dest.rglob("*.json"))
                    job.progress(None, f"{note} – {n} Datei{'en' if n != 1 else ''} geladen")
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out_path.read_text(errors="replace"),
                                       err_path.read_text(errors="replace"))


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


def _plan(acc, options: dict) -> tuple[list[tuple[str, str, datetime | None]], bool]:
    """Was soll abgerufen werden? (Art, URL, ab-Datum) und ob es ein Archiv-Auftrag ist."""
    backfill = options.get("backfill")
    if backfill:
        sel = dict(acc)
        sel.update({"stories": int(bool(backfill.get("stories"))),
                    "highlights": int(bool(backfill.get("highlights"))),
                    "posts": int(bool(backfill.get("posts")))})
        since = backfill.get("since")
        since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else None
        return [(k, u, since_dt if k == "posts" else None) for k, u in social.content_urls(sel)], True
    created = datetime.fromisoformat(acc["created_at"].replace("Z", "+00:00"))
    # Laufende Beobachtung: Beiträge erst ab kurz vor Beobachtungsbeginn
    return [(k, u, created - timedelta(days=3) if k == "posts" else None)
            for k, u in social.content_urls(acc)], False


def check_account(source_id, job: JobContext):
    account_id = job.options.get("account_id")
    with connection() as conn:
        acc = social.get_account(conn, account_id)
    if acc is None or (not acc["active"] and not job.options.get("backfill")):
        return None
    platform, handle = acc["platform"], acc["handle"]
    label = f"{social.PLATFORMS[platform]} @{handle}"
    cookies = social.cookie_path(platform)
    if not cookies.is_file():
        with connection() as conn:
            social.record_check(conn, account_id, "Keine Cookies hinterlegt",
                                "Unter Einstellungen die Cookies des Recherche-Kontos hochladen.", 0)
        return None

    plan, is_backfill = _plan(acc, job.options)
    tmp = config.DATA_DIR / "tmp" / f"account-{account_id}-{job.id or 0}-{int(time.time())}"
    # Archiv je Recherche: derselbe Account kann in mehreren Recherchen beobachtet werden
    archive = config.DATA_DIR / "social-archive" / f"projekt-{acc['project_id']}.sqlite3"
    archive.parent.mkdir(parents=True, exist_ok=True)
    timeout = config.SOCIAL_BACKFILL_TIMEOUT if is_backfill else config.SOCIAL_TIMEOUT
    new_items = 0
    problems: list[str] = []
    auth_failed = False
    try:
        for kind, url, date_min in plan:
            note = f"{label}: {'Archiv – ' if is_backfill else ''}{KIND_LABEL[kind]}s"
            job.progress(None, note, force=True)
            dest = tmp / kind
            dest.mkdir(parents=True, exist_ok=True)
            try:
                proc = run_gallery_dl(url, dest, cookies, archive, date_min, job=job, note=note,
                                      timeout=timeout)
                problem = classify(proc)
            except subprocess.TimeoutExpired:
                proc, problem = None, None
                problems.append(f"{KIND_LABEL[kind]}: Zeitlimit erreicht – bisher Geladenes ist gesichert, "
                                "erneut starten setzt fort")
            if problem == "auth":
                auth_failed = True
            if problem and problem != "error":
                problems.append({"auth": "Anmeldung abgelaufen oder blockiert",
                                 "rate": "Zu viele Anfragen – Plattform bremst",
                                 "notfound": "Account nicht gefunden oder privat"}[problem])
            elif problem == "error" and not any(dest.rglob("*.json")):
                last = (proc.stderr.strip().splitlines() or ["unbekannter Fehler"])[-1]
                problems.append(f"{KIND_LABEL[kind]}: {last[:200]}")
            # Auch nach Zeitlimit oder Teilfehlern: alles Vollständige übernehmen
            new_items += import_downloads(acc, kind, dest)
            job.check_cancel()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    prefix = "Archiv: " if is_backfill else ""
    if problems:
        status = prefix + "; ".join(dict.fromkeys(problems))
        if new_items:
            status += f" ({new_items} neu gesichert)"
    else:
        status = prefix + (f"{new_items} neu" if new_items else "nichts Neues")
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
