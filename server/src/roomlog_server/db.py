"""SQLite schema (§4.3), WAL, forward-only migrations keyed on `PRAGMA user_version`.

Plain `sqlite3`. Two external-content FTS5 tables over `segments.text`:
`segments_fts` (unicode61, diacritics kept, so `går` ≠ `gar`) for bm25 search and
`segments_tri` (trigram) for fuzzy search, both kept in sync by triggers.

v4 (ADR 0008): `dictation_spans` (what the dictation tool said happened on a device between
two instants), `tags` (facts with a `source` on a transcript row or a device time span) and
`segments` rebuilt with `device_id`, a nullable `chunk_id` (a span with no overlapping audio
still becomes a row), `span_id`, `superseded_by` and `lang NOT NULL`.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("roomlog.db")

SCHEMA_VERSION = 4

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
                        CHECK (status IN ('pending', 'segmented', 'failed')),
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
    # ADR 0008. `segments` is rebuilt (SQLite cannot drop NOT NULL on chunk_id in place); the
    # external-content FTS tables keep their index because row ids and text are unchanged,
    # only the triggers have to come back. Existing rows get `device_id` from their chunk
    # (dangling rows from before keep an empty device) and `lang` backfilled to `no`: every
    # row so far came from NB-Whisper with the worker's `language = "no"`.
    4: """
CREATE TABLE dictation_spans (
    id                  INTEGER PRIMARY KEY,
    device_id           TEXT NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    end_utc_ms          INTEGER NOT NULL,
    text                TEXT NOT NULL,
    lang                TEXT NOT NULL,
    engine              TEXT NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('raw', 'cleanup', 'edit-instruction')),
    app                 TEXT,
    window              TEXT,
    cancelled           INTEGER NOT NULL DEFAULT 0,
    origin              TEXT NOT NULL,
    body_json           TEXT NOT NULL,
    received_utc_ms     INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied')),
    applied_utc_ms      INTEGER,
    segment_id          INTEGER,
    UNIQUE (device_id, start_utc_ms)
);
CREATE INDEX dictation_spans_device_time ON dictation_spans (device_id, start_utc_ms, end_utc_ms);
CREATE INDEX dictation_spans_status ON dictation_spans (status);

CREATE TABLE segments_v4 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id            INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
    device_id           TEXT NOT NULL,
    span_id             INTEGER REFERENCES dictation_spans(id) ON DELETE CASCADE,
    superseded_by       INTEGER REFERENCES dictation_spans(id) ON DELETE SET NULL,
    idx                 INTEGER NOT NULL,
    start_utc_ms        INTEGER NOT NULL,
    end_utc_ms          INTEGER NOT NULL,
    offset_ms           INTEGER NOT NULL,
    text                TEXT NOT NULL,
    lang                TEXT NOT NULL DEFAULT 'no',
    avg_logprob         REAL,
    no_speech_prob      REAL,
    compression_ratio   REAL,
    words_json          TEXT,
    speaker             TEXT,
    model_id            TEXT NOT NULL,
    model_revision      TEXT
);
INSERT INTO segments_v4 (id, chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang,
                         avg_logprob, no_speech_prob, compression_ratio, words_json, speaker,
                         model_id, model_revision)
    SELECT s.id, s.chunk_id, COALESCE(c.device_id, ''), s.idx, s.start_utc_ms, s.end_utc_ms, s.offset_ms,
           s.text, COALESCE(s.lang, 'no'), s.avg_logprob, s.no_speech_prob, s.compression_ratio,
           s.words_json, s.speaker, s.model_id, s.model_revision
    FROM segments s LEFT JOIN chunks c ON c.id = s.chunk_id;
DROP TABLE segments;
ALTER TABLE segments_v4 RENAME TO segments;
CREATE INDEX segments_start ON segments (start_utc_ms);
CREATE INDEX segments_chunk ON segments (chunk_id, idx);
CREATE INDEX segments_device_start ON segments (device_id, start_utc_ms);
CREATE INDEX segments_span ON segments (span_id);

CREATE TABLE tags (
    id                  INTEGER PRIMARY KEY,
    target              TEXT NOT NULL CHECK (target IN ('segment', 'span')),
    segment_id          INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    device_id           TEXT,
    start_utc_ms        INTEGER,
    end_utc_ms          INTEGER,
    key                 TEXT NOT NULL,
    value               TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('deterministic', 'model')),
    origin              TEXT NOT NULL,
    created_utc_ms      INTEGER NOT NULL
);
CREATE INDEX tags_segment ON tags (segment_id, key);
CREATE INDEX tags_span ON tags (device_id, start_utc_ms, end_utc_ms, key);

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


def _dangling(conn: sqlite3.Connection) -> set[tuple]:
    """Rows failing a foreign key, as (table, rowid, parent). The constraint index that
    `foreign_key_check` also reports is left out: a table rebuild renumbers it."""
    return {(r[0], r[1], r[2]) for r in conn.execute("PRAGMA foreign_key_check").fetchall()}


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
    # transaction, so it is toggled around the whole run, and every step is checked before
    # its COMMIT: a step that adds a dangling reference is rolled back and raised. Dangling
    # rows that were there before are not the migration's doing; they are logged and kept,
    # so a start never fails on them.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        before = _dangling(conn)
        if before:
            log.warning("database has %d dangling foreign key row(s) before migration", len(before))
        for version in range(current + 1, target + 1):
            script = f"BEGIN;\n{_MIGRATIONS[version]}\nPRAGMA user_version={version};"
            try:
                conn.executescript(script)
                after = _dangling(conn)
                added = after - before
                if added:
                    raise RuntimeError(
                        f"schema migration to v{version} left {len(added)} dangling foreign key(s)")
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query: every whitespace token becomes a phrase."""
    parts = []
    for tok in q.split():
        tok = tok.replace('"', '""')
        parts.append(f'"{tok}"')
    return " ".join(parts)
