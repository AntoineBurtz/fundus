"""Beweissicherung: Fassungen, gesicherte Dateien, Manifest, Zeitstempel, Nachweis-Paket."""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from . import config, repo, tsa
from .util import human_datetime, now_iso, sha256_file

ARTIFACT_LABELS = {
    "html": "Original-HTML",
    "rendered": "Gerenderte Seite (HTML)",
    "screenshot": "Screenshot",
    "pdf": "PDF",
    "media": "Mediendatei",
    "original": "Originaldatei",
    "metadata": "Metadaten (JSON)",
}
REASONS = {
    "erstsicherung": "Erstsicherung",
    "änderung": "Änderung erkannt",
    "manuell": "Manuell neu gesichert",
    "gelöscht": "Seite nicht mehr abrufbar",
}


def normalize_text(text: str) -> str:
    return "\n".join(" ".join(line.split()) for line in (text or "").splitlines() if line.strip())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


# ------------------------------------------------------------- Fassungen

def create_version(conn: sqlite3.Connection, source_id: int, reason: str, *,
                   http_status: int | None = None, final_url: str | None = None,
                   title: str | None = None, text: str | None = None,
                   note: str | None = None) -> tuple[int, int, Path]:
    n = conn.execute(
        "SELECT COALESCE(MAX(n), 0) + 1 FROM versions WHERE source_id = ?", (source_id,)
    ).fetchone()[0]
    vid = conn.execute(
        """INSERT INTO versions(source_id, n, reason, fetched_at, http_status, final_url, title,
                                text, text_hash, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, n, reason, now_iso(), http_status, final_url, title, text,
         text_hash(text) if text is not None else None, note),
    ).lastrowid
    folder = version_dir(source_id, n)
    folder.mkdir(parents=True, exist_ok=True)
    return vid, n, folder


def version_dir(source_id: int, n: int) -> Path:
    return repo.source_dir(source_id) / f"v{n}"


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()


def get_version_n(conn: sqlite3.Connection, source_id: int, n: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE source_id = ? AND n = ?", (source_id, n)
    ).fetchone()


def list_versions(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE source_id = ? ORDER BY n DESC", (source_id,)
    ).fetchall()


def latest_content_version(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    """Letzte Fassung mit Inhalt (keine "gelöscht"-Einträge)."""
    return conn.execute(
        """SELECT * FROM versions WHERE source_id = ? AND text_hash IS NOT NULL
           ORDER BY n DESC LIMIT 1""",
        (source_id,),
    ).fetchone()


def add_note(conn: sqlite3.Connection, version_id: int, note: str) -> None:
    conn.execute(
        "UPDATE versions SET note = TRIM(COALESCE(note || char(10), '') || ?) WHERE id = ?",
        (note, version_id),
    )


# ------------------------------------------------------------- Dateien

def add_artifact(conn: sqlite3.Connection, source_id: int, version_id: int | None, kind: str,
                 path: Path, mime: str | None = None) -> int:
    path = Path(path)
    rel = str(path.resolve().relative_to(config.FILES_DIR.resolve()))
    return conn.execute(
        """INSERT INTO artifacts(source_id, version_id, kind, path, sha256, size, mime)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_id, version_id, kind, rel, sha256_file(path), path.stat().st_size,
         mime or mimetypes.guess_type(path.name)[0]),
    ).lastrowid


def artifacts_for(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    order = "CASE kind WHEN 'screenshot' THEN 0 WHEN 'pdf' THEN 1 WHEN 'html' THEN 2 " \
            "WHEN 'rendered' THEN 3 ELSE 4 END"
    return conn.execute(
        f"SELECT * FROM artifacts WHERE version_id = ? ORDER BY {order}, id", (version_id,)
    ).fetchall()


def artifacts_by_version(conn: sqlite3.Connection, source_id: int) -> dict[int, list[sqlite3.Row]]:
    out: dict[int, list[sqlite3.Row]] = {}
    for v in list_versions(conn, source_id):
        out[v["id"]] = artifacts_for(conn, v["id"])
    return out


# ------------------------------------------------------------- Manifest + Zeitstempel

def build_manifest(conn: sqlite3.Connection, version_id: int) -> tuple[Path, bytes]:
    """Schreibt manifest.json: alle Dateien dieser Fassung mit SHA-256."""
    v = get_version(conn, version_id)
    s = repo.get_source(conn, v["source_id"])
    folder = version_dir(v["source_id"], v["n"])
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "fundus_manifest": 1,
        "quelle": {"id": s["id"], "titel": s["title"], "url": s["url"]},
        "fassung": v["n"],
        "anlass": v["reason"],
        "abgerufen_utc": v["fetched_at"],
        "http_status": v["http_status"],
        "endgueltige_url": v["final_url"],
        "text_sha256": v["text_hash"],
        "dateien": [
            {"art": a["kind"], "datei": Path(a["path"]).name, "sha256": a["sha256"], "bytes": a["size"]}
            for a in artifacts_for(conn, version_id)
        ],
    }
    data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    path = folder / "manifest.json"
    path.write_bytes(data)
    conn.execute(
        "UPDATE versions SET manifest_path = ?, manifest_sha256 = ? WHERE id = ?",
        (str(path.relative_to(config.FILES_DIR)), hashlib.sha256(data).hexdigest(), version_id),
    )
    return path, data


def seal(conn_factory, version_id: int) -> None:
    """Manifest schreiben und unabhängigen Zeitstempel dafür anfordern.

    Ein fehlgeschlagener Zeitstempel (z. B. kein Internet) lässt die Sicherung
    selbst bestehen; er kann später erneut angefordert werden.
    """
    with conn_factory() as conn:
        path, data = build_manifest(conn, version_id)
    if not config.TSA_URLS:
        return
    digest = hashlib.sha256(data).digest()
    try:
        ts = tsa.stamp(digest, config.TSA_URLS)
    except tsa.TimestampError as e:
        with conn_factory() as conn:
            conn.execute("UPDATE versions SET tsa_error = ? WHERE id = ?", (str(e)[:1000], version_id))
        return
    token_path = path.with_name("manifest.tsr")
    token_path.write_bytes(ts.token)
    with conn_factory() as conn:
        conn.execute(
            """UPDATE versions SET tsa_url = ?, tsa_time = ?, tsa_token_path = ?, tsa_error = NULL
               WHERE id = ?""",
            (ts.url, ts.gen_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
             str(token_path.relative_to(config.FILES_DIR)), version_id),
        )


# ------------------------------------------------------------- Nachweis-Paket

def evidence_zip(conn: sqlite3.Connection, version_id: int) -> tuple[str, bytes]:
    """ZIP mit allen Dateien, Manifest, Zeitstempel und einer Prüfanleitung."""
    v = get_version(conn, version_id)
    s = repo.get_source(conn, v["source_id"])
    arts = artifacts_for(conn, version_id)
    buf = io.BytesIO()
    sums = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in arts:
            p = config.FILES_DIR / a["path"]
            if p.is_file():
                name = Path(a["path"]).name
                z.write(p, f"dateien/{name}")
                sums.append(f"{a['sha256']}  dateien/{name}")
        if v["manifest_path"] and (config.FILES_DIR / v["manifest_path"]).is_file():
            z.write(config.FILES_DIR / v["manifest_path"], "manifest.json")
        if v["tsa_token_path"] and (config.FILES_DIR / v["tsa_token_path"]).is_file():
            z.write(config.FILES_DIR / v["tsa_token_path"], "manifest.tsr")
        z.writestr("sha256sums.txt", "\n".join(sums) + "\n")
        z.writestr("PRUEFEN.txt", _instructions(s, v, arts))
    slug = re.sub(r"[^\w\-]+", "_", s["title"] or "quelle").strip("_")[:60] or "quelle"
    return f"nachweis_{slug}_fassung{v['n']}.zip", buf.getvalue()


def _instructions(s: Any, v: Any, arts: list) -> str:
    lines = [
        "SICHERUNGSNACHWEIS",
        "==================",
        "",
        f"Quelle:        {s['title']}",
        f"URL:           {s['url'] or '–'}",
        f"Fassung:       {v['n']} ({REASONS.get(v['reason'], v['reason'])})",
        f"Abgerufen:     {v['fetched_at']} (UTC, Uhr des Fundus-Servers)",
    ]
    if v["http_status"]:
        lines.append(f"HTTP-Status:   {v['http_status']}")
    if v["final_url"] and v["final_url"] != s["url"]:
        lines.append(f"Weitergeleitet: {v['final_url']}")
    if v["tsa_time"]:
        lines += [
            f"Zeitstempel:   {v['tsa_time']} (UTC), bestätigt von {v['tsa_url']}",
            f"Manifest SHA-256: {v['manifest_sha256']}",
        ]
    else:
        lines.append("Zeitstempel:   keiner (nur Serverzeit)")
    lines += ["", "Enthaltene Dateien:"]
    for a in arts:
        lines.append(f"  - {ARTIFACT_LABELS.get(a['kind'], a['kind'])}: {Path(a['path']).name} "
                     f"({a['size']:,} Bytes)".replace(",", "."))
    lines += [
        "",
        "SO PRÜFEN SIE DIE ECHTHEIT",
        "--------------------------",
        "1. Dateien unverändert?  (Linux/macOS, im entpackten Ordner)",
        "     sha256sum -c sha256sums.txt",
        "   Windows (PowerShell):  Get-FileHash dateien\\* -Algorithm SHA256",
        "   Die Prüfsummen müssen außerdem mit denen in manifest.json übereinstimmen.",
        "",
    ]
    if v["tsa_time"]:
        lines += [
            "2. Existierte das Manifest zum angegebenen Zeitpunkt?",
            "   Der Zeitstempel (manifest.tsr) ist eine Signatur eines unabhängigen",
            "   Zeitstempeldienstes nach RFC 3161 über die Prüfsumme von manifest.json.",
            "",
            "     openssl ts -reply -in manifest.tsr -text",
            "     openssl ts -verify -in manifest.tsr -data manifest.json -CApath /etc/ssl/certs",
            "",
            "   Bei freetsa.org statt -CApath dessen Wurzelzertifikat verwenden:",
            "     https://freetsa.org/files/cacert.pem  →  -CAfile cacert.pem",
            "",
            "   Ergebnis „Verification: OK“ bedeutet: manifest.json – und damit jede darin",
            "   per Prüfsumme aufgeführte Datei – lag spätestens zum Zeitstempel so vor.",
        ]
    return "\n".join(lines) + "\n"


def describe_tsa(v: Any) -> str:
    if v["tsa_time"]:
        host = re.sub(r"^https?://", "", v["tsa_url"] or "").split("/")[0]
        return f"{human_datetime(v['tsa_time'])} · {host}"
    return ""
