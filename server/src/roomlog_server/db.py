"""SQLite schema (§4.3), WAL, forward-only migrations keyed on `PRAGMA user_version`.

Plain `sqlite3`. Two external-content FTS5 tables over `segments.text`:
`segments_fts` (unicode61, diacritics kept, so `går` ≠ `gar`) for bm25 search and
`segments_tri` (trigram) for fuzzy search, both kept in sync by triggers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE chunks (
    id                  INTEGER PRIMARY KEY,
    sha256              TEXT NOT NULL UNIQUE,
    device_id           TEXT NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    end_utc_ms          INTEGER NOT NULL,
    duration_ms         INTEGER NOT NULL,
    path                TEXT NOT NULL,
    meta_json           TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    epoch               INTEGER NOT NULL,
    discontinuity       INTEGER NOT NULL DEFAULT 0,
    clock_synced        INTEGER NOT NULL DEFAULT 1,
    received_utc_ms     INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'done', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    transcribed_utc_ms  INTEGER,
    model_id            TEXT,
    model_revision      TEXT,
    session_id          TEXT,
    n_segments_dropped  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX chunks_start ON chunks (start_utc_ms);
CREATE INDEX chunks_device_start ON chunks (device_id, start_utc_ms);
CREATE INDEX chunks_status ON chunks (status);
CREATE INDEX chunks_session ON chunks (session_id);

CREATE TABLE segments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id            INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    idx                 INTEGER NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    end_utc_ms          INTEGER NOT NULL,
    offset_ms           INTEGER NOT NULL,
    text                TEXT NOT NULL,
    lang                TEXT,
    avg_logprob         REAL,
    no_speech_prob      REAL,
    compression_ratio   REAL,
    words_json          TEXT,
    speaker             TEXT,
    model_id            TEXT NOT NULL,
    model_revision      TEXT
);
CREATE INDEX segments_start ON segments (start_utc_ms);
CREATE INDEX segments_chunk ON segments (chunk_id, idx);

CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    gap_s           REAL NOT NULL,
    start_utc_ms    INTEGER NOT NULL,
    end_utc_ms      INTEGER NOT NULL,
    n_chunks        INTEGER NOT NULL,
    n_segments      INTEGER NOT NULL,
    closed          INTEGER NOT NULL DEFAULT 0,
    title           TEXT,
    summary         TEXT,
    multi_speaker   INTEGER
);
CREATE INDEX sessions_device_start ON sessions (device_id, start_utc_ms);
CREATE INDEX sessions_start ON sessions (start_utc_ms);

CREATE VIRTUAL TABLE segments_fts USING fts5(
    text,
    content='segments',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE VIRTUAL TABLE segments_tri USING fts5(
    text,
    content='segments',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
    INSERT INTO segments_tri(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_tri(segments_tri, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_tri(segments_tri, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
    INSERT INTO segments_tri(rowid, text) VALUES (new.id, new.text);
END;
""",
    # The startup orphan scan and `verify` look chunks up by archive path.
    2: """
CREATE INDEX chunks_path ON chunks (path);
""",
    # Raw segments (ADR 0005): `chunks` gains `kind`, `n_start`, `n_samples` and a nullable
    # `path` (a derived chunk is a slice of the raw archive, not a file). SQLite cannot drop
    # NOT NULL in place, so the table is rebuilt; `migrate` turns foreign keys off around this
    # script so dropping the old table does not cascade into `segments`.
    3: """
CREATE TABLE chunks_v3 (
    id                  INTEGER PRIMARY KEY,
    sha256              TEXT NOT NULL UNIQUE,
    kind                TEXT NOT NULL DEFAULT 'speech' CHECK (kind IN ('speech', 'derived')),
    device_id           TEXT NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    end_utc_ms          INTEGER NOT NULL,
    duration_ms         INTEGER NOT NULL,
    path                TEXT,
    n_start             INTEGER,
    n_samples           INTEGER,
    meta_json           TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    epoch               INTEGER NOT NULL,
    discontinuity       INTEGER NOT NULL DEFAULT 0,
    clock_synced        INTEGER NOT NULL DEFAULT 1,
    received_utc_ms     INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'done', 'failed')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    transcribed_utc_ms  INTEGER,
    model_id            TEXT,
    model_revision      TEXT,
    session_id          TEXT,
    n_segments_dropped  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO chunks_v3 (id, sha256, device_id, start_utc_ms, end_utc_ms, duration_ms, path,
                       meta_json, run_id, epoch, discontinuity, clock_synced, received_utc_ms,
                       status, attempts, error, transcribed_utc_ms, model_id, model_revision,
                       session_id, n_segments_dropped)
    SELECT id, sha256, device_id, start_utc_ms, end_utc_ms, duration_ms, path,
           meta_json, run_id, epoch, discontinuity, clock_synced, received_utc_ms,
           status, attempts, error, transcribed_utc_ms, model_id, model_revision,
           session_id, n_segments_dropped
    FROM chunks;
DROP TABLE chunks;
ALTER TABLE chunks_v3 RENAME TO chunks;
CREATE INDEX chunks_start ON chunks (start_utc_ms);
CREATE INDEX chunks_device_start ON chunks (device_id, start_utc_ms);
CREATE INDEX chunks_status ON chunks (status);
CREATE INDEX chunks_session ON chunks (session_id);
CREATE INDEX chunks_path ON chunks (path);
CREATE INDEX chunks_timeline ON chunks (device_id, run_id, epoch, n_start);

CREATE TABLE raw_segments (
    id                  INTEGER PRIMARY KEY,
    sha256              TEXT NOT NULL UNIQUE,
    device_id           TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    epoch               INTEGER NOT NULL,
    n_start             INTEGER NOT NULL,
    n_samples           INTEGER NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    discontinuity       INTEGER NOT NULL DEFAULT 0,
    clock_synced        INTEGER NOT NULL DEFAULT 1,
    cut_reason          TEXT NOT NULL,
    path                TEXT NOT NULL,
    meta_json           TEXT NOT NULL,
    received_utc_ms     INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'segmented')),
    segmented_utc_ms    INTEGER
);
CREATE INDEX raw_segments_timeline ON raw_segments (device_id, run_id, epoch, n_start);
CREATE INDEX raw_segments_status ON raw_segments (status);
CREATE INDEX raw_segments_path ON raw_segments (path);
CREATE INDEX raw_segments_start ON raw_segments (device_id, start_utc_ms);

CREATE TABLE raw_progress (
    device_id           TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    epoch               INTEGER NOT NULL,
    segmented_to_n      INTEGER NOT NULL,
    scanned_to_n        INTEGER NOT NULL,
    updated_utc_ms      INTEGER NOT NULL,
    PRIMARY KEY (device_id, run_id, epoch)
);
""",
}


def _casefold_contains(haystack: str | None, needle: str | None) -> bool:
    if haystack is None or needle is None:
        return False
    return needle.casefold() in haystack.casefold()


def _configure(conn: sqlite3.Connection, readonly: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # Unicode-aware substring test for fuzzy search terms too short for the trigram index
    # (SQLite's own lower()/LIKE fold ASCII only, so `MÅ` would not match `må`).
    conn.create_function("casefold_contains", 2, _casefold_contains, deterministic=True)
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    """Open the DB. Writers migrate on open; readers (CLI, MCP) open `mode=ro`."""
    p = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        _configure(conn, readonly=True)
        return conn
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    _configure(conn, readonly=False)
    migrate(conn)
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, target: int = SCHEMA_VERSION) -> None:
    """Apply migrations up to `target` (tests stop early to build an old database)."""
    current = user_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current} is newer than this code ({SCHEMA_VERSION})")
    if current >= target:
        return
    # Table rebuilds (v3) drop a table that `segments` references. With foreign keys on, SQLite
    # would run the implicit DELETE with cascades first. The pragma is a no-op inside a
    # transaction, so it is toggled around the whole run and the result is checked.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for version in range(current + 1, target + 1):
            script = f"BEGIN;\n{_MIGRATIONS[version]}\nPRAGMA user_version={version};\nCOMMIT;"
            try:
                conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"schema migration left {len(broken)} dangling foreign key(s)")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query: every whitespace token becomes a phrase."""
    parts = []
    for tok in q.split():
        tok = tok.replace('"', '""')
        parts.append(f'"{tok}"')
    return " ".join(parts)
