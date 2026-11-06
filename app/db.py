"""SQLite-Datenbank: Schema, Verbindungen, Migrationen."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    archived    INTEGER NOT NULL DEFAULT 0
);

-- Eine Quelle: Webseite, Video, Audio/Podcast, Dokument oder eigene Notiz
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,              -- web | video | audio | document | image | note
    title         TEXT NOT NULL DEFAULT '',
    url           TEXT,
    author        TEXT NOT NULL DEFAULT '',
    site_name     TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT '',
    -- drei Zeitebenen
    published_at  TEXT,                       -- wann veröffentlicht
    captured_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), -- wann gesichert
    event_date    TEXT,                       -- worauf sich der Inhalt bezieht
    event_date_end TEXT,
    -- Einordnung
    status        TEXT NOT NULL DEFAULT 'neu', -- neu | zu prüfen | verifiziert | verworfen
    credibility   INTEGER,                    -- 1..5
    notes         TEXT NOT NULL DEFAULT '',
    -- Inhalt
    text          TEXT NOT NULL DEFAULT '',   -- lesbarer Text bzw. Transkript
    transcript_origin TEXT,                   -- z. B. "Untertitel (de)" oder "Whisper small"
    content_hash  TEXT,                       -- SHA-256 des Originals
    snapshot_path TEXT,                       -- Original-HTML / Originaldatei
    media_path    TEXT,                       -- abspielbare Audio-/Videodatei
    mime_type     TEXT,
    duration      REAL,
    -- Verarbeitung
    state         TEXT NOT NULL DEFAULT 'bereit', -- wartet | läuft | bereit | fehler
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_sources_project ON sources(project_id);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    start     REAL NOT NULL,
    end       REAL NOT NULL,
    speaker   TEXT,
    text      TEXT NOT NULL,
    char_offset INTEGER NOT NULL             -- Position im sources.text
);
CREATE INDEX IF NOT EXISTS ix_segments_source ON transcript_segments(source_id, idx);

-- Markierte Textstellen (Offsets beziehen sich auf sources.text)
CREATE TABLE IF NOT EXISTS highlights (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    start_offset INTEGER NOT NULL,
    end_offset   INTEGER NOT NULL,
    quote        TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT 'gelb',
    media_time   REAL,                       -- Zeitpunkt im Audio/Video
    event_date   TEXT,                       -- optional: eigenes Datum für den Zeitstrahl
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_highlights_source ON highlights(source_id);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS source_tags (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, tag_id)
);
CREATE TABLE IF NOT EXISTS highlight_tags (
    highlight_id INTEGER NOT NULL REFERENCES highlights(id) ON DELETE CASCADE,
    tag_id       INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (highlight_id, tag_id)
);

-- Hintergrundaufgaben (Abruf, Download, Transkription)
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued', -- queued | running | done | failed | cancelled
    error       TEXT,
    options     TEXT NOT NULL DEFAULT '{}',   -- JSON, z. B. {"whisper_model": "base"}
    progress    REAL,                         -- 0..1
    eta         REAL,                         -- Restzeit in Sekunden
    note        TEXT,                         -- aktueller Arbeitsschritt
    cancel      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, id);

-- Volltextindex über Quellen, Transkript-Segmente und Markierungen
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    title, body,
    kind UNINDEXED,        -- source | segment | highlight
    ref_id UNINDEXED,
    source_id UNINDEXED,
    project_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Spalten, die nach Version 1 hinzugekommen sind (für bestehende Datenbanken)
MIGRATIONS = [
    ("sources", "transcript_origin", "TEXT"),
    ("jobs", "options", "TEXT NOT NULL DEFAULT '{}'"),
    ("jobs", "progress", "REAL"),
    ("jobs", "eta", "REAL"),
    ("jobs", "note", "TEXT"),
    ("jobs", "cancel", "INTEGER NOT NULL DEFAULT 0"),
]


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
