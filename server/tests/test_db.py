from __future__ import annotations

import json
from pathlib import Path

import pytest

from roomlog_server import db as dbmod
from roomlog_server.archive import insert_chunk_row
from roomlog_server.db import fts_query

from conftest import CONTRACT_DIR, make_sidecar


def seed_chunk(conn, device_id="oma", start_utc="2026-09-26T10:00:00.000Z", duration_s=10.0,
               sha_seed=b"a"):
    import hashlib
    body = sha_seed * 10
    meta = make_sidecar(body, device_id=device_id, start_utc=start_utc, duration_s=duration_s)
    return insert_chunk_row(conn, meta, json.dumps(meta), hashlib.sha256(body).hexdigest(),
                            "2026/09/26/x.opus")


def insert_segment(conn, chunk_id, idx, text, model_id="m1", start=0, end=1000):
    cur = conn.execute(
        """INSERT INTO segments (chunk_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, idx, start, end, 0, text, model_id),
    )
    return cur.lastrowid


def test_migration_sets_user_version_and_wal(conn):
    assert dbmod.user_version(conn) == dbmod.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chunks", "segments", "sessions", "segments_fts", "segments_tri"} <= names
    # migrating again is a no-op
    dbmod.migrate(conn)
    assert dbmod.user_version(conn) == dbmod.SCHEMA_VERSION


def test_newer_schema_refused(cfg):
    conn = dbmod.connect(cfg.db_path)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(RuntimeError):
        dbmod.connect(cfg.db_path)


def test_readonly_connection(cfg):
    w = dbmod.connect(cfg.db_path)
    w.close()
    r = dbmod.connect(cfg.db_path, readonly=True)
    with pytest.raises(Exception):
        r.execute("INSERT INTO sessions (id, device_id, gap_s, start_utc_ms, end_utc_ms, n_chunks, n_segments) VALUES ('x','d',1,0,0,0,0)")
    r.close()


def test_diacritics_are_significant(conn):
    cid = seed_chunk(conn)
    insert_segment(conn, cid, 0, "Vi går hjem nå")
    insert_segment(conn, cid, 1, "gar er ikke et ord")
    insert_segment(conn, cid, 2, "Blåbær og øl på øya")
    hits = lambda q: [r[0] for r in conn.execute(
        "SELECT text FROM segments_fts WHERE segments_fts MATCH ?", (fts_query(q),))]
    assert hits("går") == ["Vi går hjem nå"]
    assert hits("gar") == ["gar er ikke et ord"]
    assert hits("øl") == ["Blåbær og øl på øya"]
    assert hits("blåbær") == ["Blåbær og øl på øya"]  # case-folded, diacritics kept
    assert hits("blabar") == []
    assert hits("ol") == []


def test_trigram_fuzzy_hit(conn):
    cid = seed_chunk(conn)
    insert_segment(conn, cid, 0, "Vi snakket om kunstig intelligens i går")
    rows = conn.execute("SELECT text FROM segments_tri WHERE segments_tri MATCH ?",
                        (fts_query("intellig"),)).fetchall()
    assert len(rows) == 1
    # exact-token index misses a substring; trigram finds it
    rows = conn.execute("SELECT text FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("intellig"),)).fetchall()
    assert rows == []
    rows = conn.execute("SELECT text FROM segments_tri WHERE segments_tri MATCH ?",
                        (fts_query("i går"),)).fetchall()
    assert len(rows) == 1


def test_fts_in_sync_after_retranscription(conn):
    cid = seed_chunk(conn)
    sid = insert_segment(conn, cid, 0, "første transkripsjon")
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("første"),)).fetchone()[0] == 1
    # re-transcribe: delete + insert in one transaction
    conn.execute("BEGIN")
    conn.execute("DELETE FROM segments WHERE chunk_id=? AND model_id='m1'", (cid,))
    insert_segment(conn, cid, 0, "andre transkripsjon")
    conn.execute("COMMIT")
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("første"),)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM segments_tri WHERE segments_tri MATCH ?",
                        (fts_query("første"),)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("andre"),)).fetchone()[0] == 1
    # update keeps both indexes in sync too
    conn.execute("UPDATE segments SET text='tredje' WHERE chunk_id=?", (cid,))
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH 'andre'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM segments_tri WHERE segments_tri MATCH 'tredje'").fetchone()[0] == 1
    # integrity check of the external-content indexes
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES ('integrity-check')")
    conn.execute("INSERT INTO segments_tri(segments_tri) VALUES ('integrity-check')")
    # deleting the chunk cascades and removes the index entries
    conn.execute("DELETE FROM chunks WHERE id=?", (cid,))
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH 'tredje'").fetchone()[0] == 0


def test_fts_query_escaping(conn):
    cid = seed_chunk(conn)
    insert_segment(conn, cid, 0, 'han sa "hei" AND NOT (noe)')
    for q in ('"hei"', "AND", "NOT (noe)", "hei*", "-", "a:b"):
        conn.execute("SELECT text FROM segments_fts WHERE segments_fts MATCH ?", (fts_query(q),)).fetchall()


def test_contract_examples_validate():
    from roomlog_server.sidecar import validate_sidecar
    examples = sorted((CONTRACT_DIR / "examples").glob("*.json"))
    assert examples
    for p in examples:
        validate_sidecar(json.loads(p.read_text()))
    schema = json.loads((CONTRACT_DIR / "sidecar.schema.json").read_text())
    from roomlog_server.sidecar import REQUIRED
    assert set(schema["required"]) == set(REQUIRED)
