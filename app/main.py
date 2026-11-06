"""Fundus – Web-Oberfläche und API."""
from __future__ import annotations

import base64
import logging
import re
import secrets
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import config, db, evidence, repo, worker
from .diff import diff_blocks
from .ingest.watch import WATCH_CHOICES, set_watch
from .ingest.files import kind_for
from .ingest.web import looks_like_media
from .render import color_slug, render_blocks, resolve_highlights
from .util import human_date, human_datetime, timecode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if config.RUN_WORKER:
        worker.start()
    yield
    worker.stop()


app = FastAPI(title="Fundus", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _snippet(value: str | None) -> Markup:
    s = escape(value or "")
    return Markup(s.replace("[[", "<b>").replace("]]", "</b>"))


templates.env.filters.update(
    hdate=human_date, hdatetime=human_datetime, timecode=timecode,
    cslug=color_slug, snippet=_snippet,
    domain=lambda u: urlparse(u).netloc.removeprefix("www.") if u else "",
)
def _eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return "noch < 1 Min."
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"noch ca. {minutes} Min."
    return f"noch ca. {minutes / 60:.1f} Std.".replace(".", ",", 1)


def _job_options(whisper_model: str = "", use_subtitles: str | None = "1",
                 force_whisper: bool = False) -> dict:
    opts: dict = {}
    if whisper_model in config.WHISPER_MODELS:
        opts["whisper_model"] = whisper_model
    if not use_subtitles:
        opts["subtitles"] = "off"
    if force_whisper:
        opts["force_whisper"] = True
    return opts


templates.env.filters["eta"] = _eta
templates.env.filters["filesize"] = lambda n: (
    f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, round(n / 1024))} KB").replace(".", ",")
templates.env.filters["basename"] = lambda p: Path(p).name if p else ""
templates.env.globals.update(ARTIFACT_LABELS=evidence.ARTIFACT_LABELS, REASONS=evidence.REASONS,
                             WATCH_CHOICES=WATCH_CHOICES, describe_tsa=evidence.describe_tsa)
templates.env.globals.update(KINDS=repo.KINDS, STATUSES=repo.STATUSES, COLORS=repo.COLORS,
                             WHISPER_MODELS=config.WHISPER_MODELS,
                             DEFAULT_MODEL=config.WHISPER_MODEL)


# ------------------------------------------------------------ Zugangsschutz

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not config.AUTH_PASSWORD or request.url.path == "/health":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            if secrets.compare_digest(user, config.AUTH_USER) and secrets.compare_digest(
                pw, config.AUTH_PASSWORD
            ):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Fundus"'})


@app.get("/health")
def health():
    return {"ok": True}


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _404(what: str = "Nicht gefunden"):
    raise HTTPException(status_code=404, detail=what)


def back(url: str, **params: Any) -> RedirectResponse:
    q = {k: v for k, v in params.items() if v}
    return RedirectResponse(url + (("?" + urlencode(q)) if q else ""), status_code=303)


# ------------------------------------------------------------ Projekte

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.connection() as conn:
        projects = repo.list_projects(conn)
    return render(request, "home.html", projects=projects)


@app.post("/projects")
def create_project(name: str = Form(...), description: str = Form("")):
    with db.connection() as conn:
        pid = repo.create_project(conn, name, description)
    return back(f"/p/{pid}")


def _project_ctx(conn, pid: int) -> dict:
    p = repo.get_project(conn, pid)
    if p is None:
        _404("Projekt nicht gefunden")
    return {"project": p, "tags": repo.project_tags(conn, pid)}


@app.get("/p/{pid}", response_class=HTMLResponse)
def project(request: Request, pid: int, kind: str = "", status: str = "", tag: str = "",
            order: str = "captured", msg: str = ""):
    with db.connection() as conn:
        ctx = _project_ctx(conn, pid)
        sources = repo.list_sources(conn, pid, kind or None, status or None, tag or None, order)
        stags = repo.source_tags(conn, [s["id"] for s in sources])
        active = repo.active_jobs_for_project(conn, pid)
        busy = any(s["state"] in ("wartet", "läuft") for s in sources)
    return render(request, "project.html", tab="sources", sources=sources, stags=stags, active=active,
                  f={"kind": kind, "status": status, "tag": tag, "order": order},
                  busy=busy, msg=msg, **ctx)


@app.post("/p/{pid}/edit")
def edit_project(pid: int, name: str = Form(...), description: str = Form("")):
    with db.connection() as conn:
        repo.update_project(conn, pid, name=name.strip(), description=description.strip())
    return back(f"/p/{pid}")


@app.post("/p/{pid}/delete")
def delete_project(pid: int, confirm: str = Form("")):
    with db.connection() as conn:
        p = repo.get_project(conn, pid)
        if p is None:
            _404()
        if confirm.strip() != p["name"]:
            return back(f"/p/{pid}", msg="Zum Löschen den Projektnamen exakt eingeben.")
        repo.delete_project(conn, pid)
    return back("/")


@app.get("/p/{pid}/timeline", response_class=HTMLResponse)
def project_timeline(request: Request, pid: int, basis: str = "event"):
    with db.connection() as conn:
        ctx = _project_ctx(conn, pid)
        entries = repo.timeline(conn, pid, basis)
    groups: list[dict] = []
    for e in entries:
        key = (e["date"] or "")[:4] or "Ohne Datum"
        if not groups or groups[-1]["key"] != key:
            groups.append({"key": key, "entries": []})
        groups[-1]["entries"].append(e)
    return render(request, "timeline.html", tab="timeline", groups=groups, basis=basis, **ctx)


@app.get("/p/{pid}/highlights", response_class=HTMLResponse)
def project_highlights(request: Request, pid: int, color: str = "", tag: str = ""):
    with db.connection() as conn:
        ctx = _project_ctx(conn, pid)
        hls = repo.project_highlights(conn, pid, color or None, tag or None)
        htags = repo.highlight_tags(conn, [h["id"] for h in hls])
    return render(request, "highlights.html", tab="highlights", highlights=hls, htags=htags,
                  f={"color": color, "tag": tag}, **ctx)


@app.get("/p/{pid}/export.md")
def export_markdown(pid: int):
    """Dossier: alle Quellen mit Metadaten und Markierungen als Markdown."""
    with db.connection() as conn:
        p = repo.get_project(conn, pid) or _404()
        sources = repo.list_sources(conn, pid, order="event")
        stags = repo.source_tags(conn, [s["id"] for s in sources])
        lines = [f"# {p['name']}", ""]
        if p["description"]:
            lines += [p["description"], ""]
        for n, s in enumerate(reversed(sources), 1):
            full = repo.get_source(conn, s["id"])
            hls = repo.list_highlights(conn, s["id"])
            htags = repo.highlight_tags(conn, [h["id"] for h in hls])
            lines.append(f"## [{n}] {s['title'] or s['url'] or 'Ohne Titel'}")
            meta = [repo.KINDS[s["kind"]]]
            if s["author"]:
                meta.append(s["author"])
            if s["site_name"]:
                meta.append(s["site_name"])
            if s["published_at"]:
                meta.append(f"veröffentlicht {human_date(s['published_at'])}")
            if s["event_date"]:
                meta.append(f"betrifft {human_date(s['event_date'])}")
            meta.append(f"gesichert {human_datetime(s['captured_at'])}")
            lines.append("*" + " · ".join(meta) + "*  ")
            if s["url"]:
                lines.append(f"<{s['url']}>  ")
            lines.append(f"Status: {s['status']}" + (f" · Glaubwürdigkeit {s['credibility']}/5" if s["credibility"] else ""))
            if full["content_hash"]:
                lines.append(f"SHA-256: `{full['content_hash']}`")
            if stags.get(s["id"]):
                lines.append("Tags: " + ", ".join(stags[s["id"]]))
            if full["notes"]:
                lines += ["", full["notes"]]
            for h in hls:
                stamp = f" [{timecode(h['media_time'])}]" if h["media_time"] is not None else ""
                lines += ["", "> " + h["quote"].replace("\n", "\n> ") + stamp]
                extra = []
                if h["note"]:
                    extra.append(h["note"])
                if htags.get(h["id"]):
                    extra.append("Tags: " + ", ".join(htags[h["id"]]))
                if extra:
                    lines.append("")
                    lines.append("— " + " · ".join(extra))
            lines.append("")
    name = re.sub(r"[^\w\-]+", "_", p["name"]).strip("_") or "projekt"
    return PlainTextResponse(
        "\n".join(lines), media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
    )


# ------------------------------------------------------------ Quellen anlegen

@app.post("/p/{pid}/sources/url")
def add_url(pid: int, url: str = Form(...), mode: str = Form("auto"),
            event_date: str = Form(""), tags: str = Form(""),
            whisper_model: str = Form(""), use_subtitles: str | None = Form(None),
            options_shown: str = Form("")):
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    as_media = mode == "media" or (mode == "auto" and looks_like_media(url))
    try:
        with db.connection() as conn:
            sid = repo.create_source(conn, pid, "video" if as_media else "web", url=url,
                                     event_date=event_date or None, state="wartet")
            if tags:
                repo.set_source_tags(conn, sid, tags)
            # Ohne aufgeklappte Optionen (Bookmarklet, alte Formulare) gilt: Untertitel nutzen
            subs = use_subtitles if options_shown else "1"
            repo.enqueue_job(conn, sid, "media" if as_media else "web",
                             _job_options(whisper_model, subs))
    except ValueError as e:
        return back(f"/p/{pid}", msg=str(e))
    worker.wake()
    return back(f"/p/{pid}")


@app.post("/p/{pid}/sources/upload")
async def add_upload(pid: int, files: list[UploadFile] = File(...),
                     event_date: str = Form(""), tags: str = Form(""),
                     whisper_model: str = Form("")):
    created = []
    for up in files:
        if not up.filename:
            continue
        kind = kind_for(up.filename, up.content_type)
        with db.connection() as conn:
            sid = repo.create_source(conn, pid, kind, title=Path(up.filename).stem,
                                     mime_type=up.content_type, event_date=event_date or None,
                                     state="wartet")
            if tags:
                repo.set_source_tags(conn, sid, tags)
        folder = repo.source_dir(sid)
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", up.filename)[-120:] or "datei"
        dest = folder / safe
        with open(dest, "wb") as out:
            while chunk := await up.read(1 << 20):
                out.write(chunk)
        with db.connection() as conn:
            repo.update_source(conn, sid, snapshot_path=str(dest.relative_to(config.FILES_DIR)))
            repo.enqueue_job(conn, sid, "file", _job_options(whisper_model))
        created.append(sid)
    worker.wake()
    if len(created) == 1:
        return back(f"/s/{created[0]}")
    return back(f"/p/{pid}")


@app.post("/p/{pid}/sources/note")
def add_note(pid: int, title: str = Form(...), text: str = Form(""),
             event_date: str = Form(""), tags: str = Form("")):
    try:
        with db.connection() as conn:
            sid = repo.create_source(conn, pid, "note", title=title.strip(), text=text,
                                     event_date=event_date or None)
            if tags:
                repo.set_source_tags(conn, sid, tags)
    except ValueError as e:
        return back(f"/p/{pid}", msg=str(e))
    return back(f"/s/{sid}")


# Schnell-Erfassung (Bookmarklet / Teilen-Menü)
@app.get("/add", response_class=HTMLResponse)
def quick_add(request: Request, url: str = "", title: str = "", text: str = ""):
    if not url and text:  # Android-Teilen liefert die URL oft im Text
        m = re.search(r"https?://\S+", text)
        url = m.group(0) if m else ""
    with db.connection() as conn:
        projects = repo.list_projects(conn)
    return render(request, "quick_add.html", url=url, title=title, projects=projects)


@app.post("/add")
def quick_add_post(url: str = Form(...), project_id: str = Form(""), new_project: str = Form(""),
                   mode: str = Form("auto"), tags: str = Form("")):
    with db.connection() as conn:
        pid = repo.create_project(conn, new_project) if new_project.strip() else int(project_id)
    return add_url(pid, url=url, mode=mode, event_date="", tags=tags, whisper_model="",
                   use_subtitles="1", options_shown="")


# ------------------------------------------------------------ Quelle ansehen

@app.get("/s/{sid}", response_class=HTMLResponse)
def source(request: Request, sid: int, t: float | None = None, h: int | None = None, msg: str = ""):
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404("Quelle nicht gefunden")
        p = repo.get_project(conn, s["project_id"])
        segments = repo.get_segments(conn, sid)
        hls, orphans = resolve_highlights(s["text"], repo.list_highlights(conn, sid))
        htags = repo.highlight_tags(conn, [x["id"] for x in hls + orphans])
        tags = repo.source_tags(conn, [sid])[sid]
        jobs = repo.source_jobs(conn, sid)
        active = repo.active_job(conn, sid)
        versions = evidence.list_versions(conn, sid)
        varts = evidence.artifacts_by_version(conn, sid)
    latest_arts = {a["kind"]: a for v in reversed(versions) for a in varts.get(v["id"], [])}
    blocks = render_blocks(s["text"], hls, segments)
    return render(request, "source.html", s=s, project=p, blocks=blocks, highlights=hls,
                  orphans=orphans, htags=htags, tags=tags, jobs=jobs, segments=segments, active=active,
                  versions=versions, varts=varts, latest_arts=latest_arts,
                  start_time=t, focus=h, msg=msg)


@app.get("/s/{sid}/state")
def source_state(sid: int):
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        job = repo.active_job(conn, sid)
    out = {"state": s["state"], "error": s["error"]}
    if job is not None:
        out.update(job=job["type"], job_status=job["status"], note=job["note"],
                   progress=job["progress"], eta=job["eta"], eta_text=_eta(job["eta"]),
                   cancelling=bool(job["cancel"]))
    return out


@app.post("/s/{sid}/edit")
def edit_source(sid: int, title: str = Form(""), url: str = Form(""), author: str = Form(""),
                site_name: str = Form(""), published_at: str = Form(""), event_date: str = Form(""),
                event_date_end: str = Form(""), status: str = Form("neu"),
                credibility: str = Form(""), notes: str = Form(""), description: str = Form(""),
                tags: str = Form("")):
    try:
        with db.connection() as conn:
            repo.update_source(
                conn, sid, title=title.strip(), url=url.strip() or None, author=author.strip(),
                site_name=site_name.strip(), published_at=published_at, event_date=event_date,
                event_date_end=event_date_end, status=status, credibility=credibility,
                notes=notes, description=description,
            )
            repo.set_source_tags(conn, sid, tags)
    except ValueError as e:
        return back(f"/s/{sid}", msg=str(e))
    return back(f"/s/{sid}", msg="Gespeichert")


@app.post("/s/{sid}/text")
def edit_text(sid: int, text: str = Form("")):
    """Text einer Notiz bearbeiten (Markierungen werden neu verortet)."""
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        if s["kind"] != "note":
            return back(f"/s/{sid}", msg="Nur Notizen sind direkt bearbeitbar.")
        repo.update_source(conn, sid, text=text.replace("\r\n", "\n"))
    return back(f"/s/{sid}")


@app.post("/s/{sid}/delete")
def delete_source(sid: int):
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        repo.delete_source(conn, sid)
    return back(f"/p/{s['project_id']}")


@app.post("/s/{sid}/run/{action}")
def rerun(sid: int, action: str, whisper_model: str = Form("")):
    """Abruf wiederholen, als Medium laden, neu transkribieren oder abbrechen."""
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        if action == "retry":
            job = {"web": "web", "video": "media", "audio": "media"}.get(s["kind"], "file")
            if job == "media" and not s["url"]:
                job = "file"
            if job == "file" and not s["snapshot_path"]:
                return back(f"/s/{sid}", msg="Keine Datei vorhanden.")
            repo.enqueue_job(conn, sid, job, _job_options(whisper_model))
        elif action == "media" and s["url"]:
            repo.enqueue_job(conn, sid, "media", _job_options(whisper_model))
        elif action == "transcribe" and s["media_path"]:
            repo.enqueue_job(conn, sid, "transcribe", _job_options(whisper_model, force_whisper=True))
        elif action == "cancel":
            repo.request_cancel(conn, sid)
        elif action == "recheck" and s["url"] and s["kind"] == "web":
            repo.enqueue_job(conn, sid, "recheck", {"force": True})
        elif action == "archive" and s["url"]:
            repo.enqueue_job(conn, sid, "archive")
        else:
            return back(f"/s/{sid}", msg="Aktion nicht möglich.")
    worker.wake()
    return back(f"/s/{sid}")


@app.post("/s/{sid}/watch")
def watch(sid: int, hours: int = Form(0)):
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        if hours and not s["url"]:
            return back(f"/s/{sid}", msg="Nur Quellen mit URL können überwacht werden.")
        set_watch(conn, sid, hours if hours in WATCH_CHOICES and hours > 0 else None)
    return back(f"/s/{sid}", msg="Überwachung " + ("eingeschaltet" if hours else "ausgeschaltet"))


@app.get("/s/{sid}/v/{n}", response_class=HTMLResponse)
def version(request: Request, sid: int, n: int, msg: str = ""):
    """Eine Fassung ansehen – mit Unterschieden zur vorherigen."""
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
        v = evidence.get_version_n(conn, sid, n) or _404("Fassung nicht gefunden")
        p = repo.get_project(conn, s["project_id"])
        prev = conn.execute(
            """SELECT * FROM versions WHERE source_id = ? AND n < ? AND text_hash IS NOT NULL
               ORDER BY n DESC LIMIT 1""", (sid, n)).fetchone()
        arts = evidence.artifacts_for(conn, v["id"])
        total = conn.execute("SELECT MAX(n) FROM versions WHERE source_id = ?", (sid,)).fetchone()[0]
        if s["watch_alert"] and n == total:
            conn.execute("UPDATE sources SET watch_alert = 0 WHERE id = ?", (sid,))
    blocks, stats = diff_blocks(prev["text"] or "", v["text"] or "") if prev and v["text"] is not None else ([], None)
    return render(request, "version.html", s=s, v=v, prev=prev, arts=arts, project=p,
                  blocks=blocks, stats=stats, total=total, msg=msg)


@app.post("/s/{sid}/v/{n}/seal")
def reseal(sid: int, n: int):
    with db.connection() as conn:
        v = evidence.get_version_n(conn, sid, n) or _404()
        repo.enqueue_job(conn, sid, "seal", {"version_id": v["id"]})
    worker.wake()
    return back(f"/s/{sid}/v/{n}", msg="Zeitstempel wird angefordert …")


@app.get("/s/{sid}/v/{n}/nachweis.zip")
def evidence_zip(sid: int, n: int):
    with db.connection() as conn:
        v = evidence.get_version_n(conn, sid, n) or _404()
        name, data = evidence.evidence_zip(conn, v["id"])
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/a/{aid}")
def artifact(aid: int, download: int = 0):
    """Gesicherte Datei ausliefern. HTML nur in einer Sandbox ohne Skripte."""
    with db.connection() as conn:
        a = conn.execute("SELECT * FROM artifacts WHERE id = ?", (aid,)).fetchone() or _404()
    path = _safe_file(a["path"])
    headers = _sandbox_headers(a["mime"] or path.name)
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(path, headers=headers, media_type=a["mime"] or None)


@app.get("/s/{sid}/snapshot")
def snapshot(sid: int):
    """Gesichertes Original anzeigen. HTML läuft in einer Sandbox ohne Skripte."""
    with db.connection() as conn:
        s = repo.get_source(conn, sid) or _404()
    if not s["snapshot_path"]:
        _404("Kein Original gesichert")
    path = _safe_file(s["snapshot_path"])
    return FileResponse(path, headers=_sandbox_headers(s["mime_type"] or path.name),
                        media_type=s["mime_type"] or None)


@app.get("/files/{rel:path}")
def files(rel: str):
    path = _safe_file(rel)
    return FileResponse(path, headers=_sandbox_headers(path.name))


def _sandbox_headers(mime_or_name: str) -> dict:
    """Gesicherte Webseiten dürfen kein JavaScript ausführen (Sandbox).

    PDFs und Bilder bekommen keine Sandbox, sonst zeigt der Browser sie nicht an.
    """
    m = (mime_or_name or "").lower()
    if "html" in m or m.endswith((".htm", ".svg")) or "svg" in m or "xml" in m:
        return {"Content-Security-Policy": "sandbox; default-src 'none'; img-src * data:; "
                                           "style-src * 'unsafe-inline'; font-src * data:"}
    return {"X-Content-Type-Options": "nosniff"}


def _safe_file(rel: str) -> Path:
    root = config.FILES_DIR.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        _404("Datei nicht gefunden")
    return path


# ------------------------------------------------------------ Markierungen (JSON)

class HighlightIn(BaseModel):
    source_id: int
    start: int
    end: int
    note: str = ""
    color: str = "gelb"
    tags: str = ""
    event_date: str | None = None


class HighlightPatch(BaseModel):
    note: str | None = None
    color: str | None = None
    tags: str | None = None
    event_date: str | None = None


@app.post("/api/highlights")
def api_add_highlight(body: HighlightIn):
    try:
        with db.connection() as conn:
            hid = repo.add_highlight(conn, body.source_id, body.start, body.end, body.note,
                                     body.color, body.tags, body.event_date)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"id": hid}


@app.patch("/api/highlights/{hid}")
def api_patch_highlight(hid: int, body: HighlightPatch):
    try:
        with db.connection() as conn:
            repo.update_highlight(conn, hid, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.delete("/api/highlights/{hid}")
def api_delete_highlight(hid: int):
    with db.connection() as conn:
        repo.delete_highlight(conn, hid)
    return {"ok": True}


# ------------------------------------------------------------ Suche

@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", project: int | None = None):
    with db.connection() as conn:
        results = repo.search(conn, q, project) if q.strip() else []
        for r in results:
            params = {}
            if r.get("time") is not None:
                params["t"] = round(r["time"], 2)
            if r["kind"] == "highlight":
                params["h"] = r["ref_id"]
            r["href"] = f"/s/{r['source_id']}" + (("?" + urlencode(params)) if params else "") + (
                f"#hl-{r['ref_id']}" if r["kind"] == "highlight" else "")
        projects = repo.list_projects(conn)
        p = repo.get_project(conn, project) if project else None
    return render(request, "search.html", q=q, results=results, projects=projects,
                  project=p, project_id=project)
