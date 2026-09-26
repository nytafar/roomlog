"""Sessions (§4.5): gap-and-island per device, full rebuild in one transaction.

A new island starts when `start − LAG(end) > gap_s`. The session id is
`<device_id>_<first chunk's start_utc compact>`, so ids are stable across rebuilds and a
late-arriving chunk only shifts the session it falls into. A session is `closed` once
`now − end_utc > gap_s`; a closed session can still absorb a late chunk on the next rebuild.
"""

from __future__ import annotations

import sqlite3

from .times import ms_to_compact, now_ms

_ISLANDS = """
WITH ordered AS (
    SELECT id, device_id, start_utc_ms, end_utc_ms,
           LAG(end_utc_ms) OVER (PARTITION BY device_id ORDER BY start_utc_ms, id) AS prev_end
    FROM chunks
),
flagged AS (
    SELECT id, device_id, start_utc_ms, end_utc_ms,
           CASE WHEN prev_end IS NULL OR start_utc_ms - prev_end > ? THEN 1 ELSE 0 END AS is_new
    FROM ordered
),
islands AS (
    SELECT id, device_id, start_utc_ms, end_utc_ms,
           SUM(is_new) OVER (PARTITION BY device_id ORDER BY start_utc_ms, id
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS island
    FROM flagged
)
SELECT id, device_id, start_utc_ms, end_utc_ms, island FROM islands
ORDER BY device_id, island, start_utc_ms, id
"""


def rebuild_sessions(conn: sqlite3.Connection, gap_s: float, now: int | None = None) -> int:
    """Recompute every session from `chunks`. Returns the number of sessions."""
    gap_ms = int(round(gap_s * 1000))
    now = now_ms() if now is None else now
    rows = conn.execute(_ISLANDS, (gap_ms,)).fetchall()

    sessions: list[dict] = []
    current: dict | None = None
    key: tuple[str, int] | None = None
    for r in rows:
        k = (r["device_id"], r["island"])
        if k != key:
            current = {
                "id": f"{r['device_id']}_{ms_to_compact(r['start_utc_ms'])}",
                "device_id": r["device_id"],
                "start_utc_ms": r["start_utc_ms"],
                "end_utc_ms": r["end_utc_ms"],
                "chunk_ids": [],
            }
            sessions.append(current)
            key = k
        assert current is not None
        current["chunk_ids"].append(r["id"])
        current["end_utc_ms"] = max(current["end_utc_ms"], r["end_utc_ms"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        old = {r["id"]: r for r in conn.execute("SELECT id, title, summary, multi_speaker FROM sessions")}
        conn.execute("DELETE FROM sessions")
        # A temp table instead of `IN (?, ...)`: SQLite caps bound variables at 32,766 and a
        # long session can hold more chunks than that.
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS session_assign (chunk_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL)")
        conn.execute("DELETE FROM temp.session_assign")
        conn.executemany(
            "INSERT INTO temp.session_assign (chunk_id, session_id) VALUES (?, ?)",
            ((cid, s["id"]) for s in sessions for cid in s["chunk_ids"]),
        )
        conn.execute(
            "UPDATE chunks SET session_id = (SELECT session_id FROM temp.session_assign a WHERE a.chunk_id = chunks.id)"
        )
        seg_counts = {
            r["session_id"]: r["n"]
            for r in conn.execute(
                """SELECT a.session_id, count(*) AS n FROM segments s
                   JOIN temp.session_assign a ON a.chunk_id = s.chunk_id GROUP BY a.session_id"""
            )
        }
        for s in sessions:
            ids = s["chunk_ids"]
            n_segments = seg_counts.get(s["id"], 0)
            prev = old.get(s["id"])
            closed = 1 if now - s["end_utc_ms"] > gap_ms else 0
            conn.execute(
                """INSERT INTO sessions (id, device_id, gap_s, start_utc_ms, end_utc_ms, n_chunks,
                                         n_segments, closed, title, summary, multi_speaker)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    s["id"], s["device_id"], gap_s, s["start_utc_ms"], s["end_utc_ms"],
                    len(ids), n_segments, closed,
                    prev["title"] if prev else None,
                    prev["summary"] if prev else None,
                    prev["multi_speaker"] if prev else None,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(sessions)
