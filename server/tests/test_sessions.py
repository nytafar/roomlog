from __future__ import annotations

import hashlib
import json

from roomlog_server.archive import insert_chunk_row
from roomlog_server.sessions import rebuild_sessions
from roomlog_server.times import iso_to_ms, ms_to_iso

from conftest import make_sidecar

T0 = iso_to_ms("2026-09-26T10:00:00.000Z")
_n = [0]


def add_chunk(conn, start_ms: int, duration_s: float = 10.0, device_id: str = "oma") -> int:
    _n[0] += 1
    body = f"{device_id}-{start_ms}-{_n[0]}".encode()
    meta = make_sidecar(body, device_id=device_id, start_utc=ms_to_iso(start_ms), duration_s=duration_s)
    return insert_chunk_row(conn, meta, json.dumps(meta), hashlib.sha256(body).hexdigest(), f"p/{_n[0]}.opus")


def sessions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY device_id, start_utc_ms")]


def test_islands_split_only_when_gap_exceeds_gap_s(conn):
    gap = 300.0
    a = add_chunk(conn, T0, 10)                       # ends T0+10s
    b = add_chunk(conn, T0 + 10_000 + 300_000, 10)    # gap exactly 300 s → same session
    c = add_chunk(conn, T0 + 20_000 + 300_000 + 300_001, 10)  # gap 300.001 s → new session
    n = rebuild_sessions(conn, gap, now=T0)
    assert n == 2
    s = sessions(conn)
    assert s[0]["id"] == "oma_20260926T100000000Z"
    assert s[0]["n_chunks"] == 2
    assert s[0]["end_utc_ms"] == T0 + 320_000
    assert s[1]["n_chunks"] == 1
    assert s[1]["gap_s"] == gap
    rows = {r["id"]: r["session_id"] for r in conn.execute("SELECT id, session_id FROM chunks")}
    assert rows[a] == rows[b] == s[0]["id"]
    assert rows[c] == s[1]["id"]


def test_partitioned_by_device(conn):
    add_chunk(conn, T0, 10, device_id="oma")
    add_chunk(conn, T0 + 5_000, 10, device_id="pi-work")
    add_chunk(conn, T0 + 20_000, 10, device_id="oma")
    rebuild_sessions(conn, 300, now=T0)
    s = sessions(conn)
    assert [x["device_id"] for x in s] == ["oma", "pi-work"]
    assert s[0]["id"] == "oma_20260926T100000000Z"
    assert s[1]["id"] == "pi-work_20260926T100005000Z"
    assert s[0]["n_chunks"] == 2


def test_late_chunk_merges_islands_and_keeps_ids_stable(conn):
    add_chunk(conn, T0, 10)
    add_chunk(conn, T0 + 500_000, 10)  # 490 s after the first ends → separate
    rebuild_sessions(conn, 300, now=T0 + 10_000_000)
    s = sessions(conn)
    assert len(s) == 2
    assert all(x["closed"] == 1 for x in s)
    first_id = s[0]["id"]
    # a late chunk from the spool backlog bridges the gap: closed sessions absorb it
    add_chunk(conn, T0 + 250_000, 10)  # ends 260 s; 240 s before the next
    rebuild_sessions(conn, 300, now=T0 + 10_000_000)
    s = sessions(conn)
    assert len(s) == 1
    assert s[0]["id"] == first_id
    assert s[0]["n_chunks"] == 3
    assert s[0]["end_utc_ms"] == T0 + 510_000


def test_late_earlier_chunk_shifts_session_id(conn):
    add_chunk(conn, T0, 10)
    rebuild_sessions(conn, 300, now=T0)
    assert sessions(conn)[0]["id"] == "oma_20260926T100000000Z"
    add_chunk(conn, T0 - 60_000, 10)
    rebuild_sessions(conn, 300, now=T0)
    s = sessions(conn)
    assert len(s) == 1
    assert s[0]["id"] == "oma_20260926T095900000Z"
    assert conn.execute("SELECT count(*) FROM chunks WHERE session_id IS NULL").fetchone()[0] == 0


def test_closing_rule(conn):
    add_chunk(conn, T0, 10)
    rebuild_sessions(conn, 300, now=T0 + 10_000 + 300_000)
    assert sessions(conn)[0]["closed"] == 0
    rebuild_sessions(conn, 300, now=T0 + 10_000 + 300_001)
    assert sessions(conn)[0]["closed"] == 1


def test_rebuild_counts_segments_and_keeps_titles(conn):
    cid = add_chunk(conn, T0, 10)
    conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) VALUES (?,'oma',0,?,?,0,'hei','m')", (cid, T0, T0 + 1000))
    rebuild_sessions(conn, 300, now=T0)
    conn.execute("UPDATE sessions SET title='Morgenmøte'")
    rebuild_sessions(conn, 300, now=T0)
    s = sessions(conn)[0]
    assert s["n_segments"] == 1
    assert s["title"] == "Morgenmøte"


def test_empty_db(conn):
    assert rebuild_sessions(conn, 300) == 0
