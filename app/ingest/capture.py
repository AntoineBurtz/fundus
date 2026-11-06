"""Seite wie im Browser öffnen: Screenshot, PDF und gerendertes HTML sichern.

Läuft mit Playwright + Chromium ohne Bildschirm. Fängt auch Seiten ein, deren
Inhalt erst per JavaScript geladen wird.
"""
from __future__ import annotations

import logging
import re

from .. import config, evidence, repo
from ..db import connection
from ..jobs import JobContext

log = logging.getLogger("fundus.capture")

CONSENT_RE = re.compile(
    r"^\s*(alle[s]? akzeptieren|akzeptieren|alle zulassen|alle cookies akzeptieren|zustimmen"
    r"|einverstanden|accept all|accept|allow all|i agree|agree)\b",
    re.I,
)

SCROLL_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let last = 0;
  for (let i = 0; i < 40; i++) {
    window.scrollBy(0, Math.round(window.innerHeight * 0.9));
    await sleep(150);
    const h = document.documentElement.scrollHeight;
    if (window.scrollY + window.innerHeight >= h - 4 || h > 40000) break;
    last = h;
  }
  window.scrollTo(0, 0);
  await sleep(300);
}
"""


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _accept_cookies(page) -> bool:
    for frame in page.frames:
        try:
            btn = frame.get_by_role("button", name=CONSENT_RE).first
            if btn.count() and btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def capture(source_id: int, job: JobContext):
    version_id = job.options.get("version_id")
    follow_up = [("seal", {"version_id": version_id})] if version_id else []
    with connection() as conn:
        src = repo.get_source(conn, source_id)
        v = evidence.get_version(conn, version_id) if version_id else None
    if v is None:
        return follow_up
    if not config.CAPTURE:
        return follow_up
    if not playwright_available():
        with connection() as conn:
            evidence.add_note(conn, version_id, "Screenshot/PDF übersprungen: Playwright nicht installiert")
        return follow_up

    from playwright.sync_api import sync_playwright

    folder = evidence.version_dir(source_id, v["n"])
    url = v["final_url"] or src["url"]
    job.progress(None, "Seite im Browser öffnen", force=True)
    notes = []
    rendered_html = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=config.CHROMIUM_PATH,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900}, user_agent=config.USER_AGENT,
                    locale="de-DE", timezone_id="Europe/Berlin", device_scale_factor=1,
                )
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=45_000)
                except Exception:
                    page.wait_for_load_state("load", timeout=15_000)
                    notes.append("Seite lädt dauerhaft nach (networkidle nicht erreicht)")
                if config.CAPTURE_ACCEPT_COOKIES and _accept_cookies(page):
                    notes.append("Cookie-Hinweis automatisch bestätigt")
                job.check_cancel()
                page.evaluate(SCROLL_JS)  # lädt Bilder, die erst beim Scrollen kommen
                try:
                    page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
                job.progress(None, "Screenshot und PDF erstellen", force=True)
                shot = folder / "screenshot.png"
                page.screenshot(path=str(shot), full_page=True, timeout=60_000)
                rendered_html = page.content()
                (folder / "gerendert.html").write_text(rendered_html, encoding="utf-8")
                pdf = folder / "seite.pdf"
                page.emulate_media(media="screen")
                page.pdf(path=str(pdf), format="A4", print_background=True,
                         margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"})
            finally:
                browser.close()
    except Exception as e:
        if type(e).__name__ == "Cancelled":
            raise
        log.warning("Screenshot für Quelle %s fehlgeschlagen: %s", source_id, e)
        notes.append(f"Screenshot/PDF fehlgeschlagen: {type(e).__name__}: {str(e)[:300]}")

    with connection() as conn:
        for kind, name, mime in (("screenshot", "screenshot.png", "image/png"),
                                 ("pdf", "seite.pdf", "application/pdf"),
                                 ("rendered", "gerendert.html", "text/html")):
            path = folder / name
            if path.is_file():
                evidence.add_artifact(conn, source_id, version_id, kind, path, mime)
        if notes:
            evidence.add_note(conn, version_id, "; ".join(notes))
        if rendered_html:
            _maybe_use_rendered_text(conn, source_id, v, rendered_html)
    return follow_up


def _maybe_use_rendered_text(conn, source_id: int, v, rendered_html: str) -> None:
    """JavaScript-Seiten: Liefert die gerenderte Seite deutlich mehr Text, wird er übernommen."""
    from .web import extract_html

    meta = extract_html(rendered_html, v["final_url"])
    new_text = meta["text"]
    old_text = v["text"] or ""
    if len(new_text) < max(300, len(old_text) * 1.5):
        return
    conn.execute("UPDATE versions SET text = ?, text_hash = ? WHERE id = ?",
                 (new_text, evidence.text_hash(new_text), v["id"]))
    evidence.add_note(conn, v["id"], "Text aus der gerenderten Seite übernommen (JavaScript-Inhalt)")
    src = repo.get_source(conn, source_id)
    has_highlights = conn.execute(
        "SELECT 1 FROM highlights WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    # Lesetext nur ersetzen, wenn er aus genau dieser Fassung stammt und unmarkiert ist
    if v["n"] == 1 and not has_highlights and len(src["text"]) <= len(old_text) + 1:
        fields = {"text": new_text}
        if meta["title"] and not src["title"]:
            fields["title"] = meta["title"]
        repo.update_source(conn, source_id, **fields)
