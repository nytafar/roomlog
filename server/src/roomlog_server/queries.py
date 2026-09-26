"""Read-only queries shared by the CLI and the MCP tools (§4.6). No raw SQL leaves this file."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import fts_query
from .times import ms_to_hms, ms_to_iso, now_ms


def _segment_row(r: sqlite3.Row, score: float | None = None) -> dict[str, Any]:
    d = {
        "segment_id": r["id"],
        "chunk_id": r["chunk_id"],
        "session_id": r["session_id"],
        "device_id": r["device_id"],
        "start_utc": ms_to_iso(r["start_utc_ms"]),
        "end_utc": ms_to_iso(r["end_utc_ms"]),
        "start_utc_ms": r["start_utc_ms"],
        "end_utc_ms": r["end_utc_ms"],
        "text": r["text"],
        "lang": r["lang"],
        "model_id": r["model_id"],
    }
    if score is not None:
        d["score"] = score
    return d


def search(conn: sqlite3.Connection, query: str, from_ms: int | None = None, to_ms: int | None = None,
           device_id: str | None = None, limit: int = 10, offset: int = 0,
           fuzzy: bool = False) -> list[dict[str, Any]]:
    """bm25 over `segments_fts`, or trigram substring match over `segments_tri` with `fuzzy`."""
    q = fts_query(query)
    if not q:
        return []
    table = "segments_tri" if fuzzy else "segments_fts"
    where = [f"{table} MATCH ?"]
    params: list[Any] = [q]
    if from_ms is not None:
        where.append("s.start_utc_ms >= ?")
        params.append(from_ms)
    if to_ms is not None:
        where.append("s.start_utc_ms < ?")
        params.append(to_ms)
    if device_id:
        where.append("c.device_id = ?")
        params.append(device_id)
    sql = f"""
        SELECT s.id, s.chunk_id, s.start_utc_ms, s.end_utc_ms, s.text, s.lang, s.model_id,
               c.device_id, c.session_id, bm25({table}) AS score
        FROM {table} f
        JOIN segments s ON s.id = f.rowid
        JOIN chunks c ON c.id = s.chunk_id
        WHERE {" AND ".join(where)}
        ORDER BY score, s.start_utc_ms
        LIMIT ? OFFSET ?
    """
    params += [max(1, min(int(limit), 500)), max(0, int(offset))]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower() or "syntax" in str(e).lower():
            return []
        raise
    return [_segment_row(r, score=r["score"]) for r in rows]


def _session_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": r["id"],
        "device_id": r["device_id"],
        "start_utc": ms_to_iso(r["start_utc_ms"]),
        "end_utc": ms_to_iso(r["end_utc_ms"]),
        "start_utc_ms": r["start_utc_ms"],
        "end_utc_ms": r["end_utc_ms"],
        "duration_s": round((r["end_utc_ms"] - r["start_utc_ms"]) / 1000, 3),
        "n_chunks": r["n_chunks"],
        "n_segments": r["n_segments"],
        "closed": bool(r["closed"]),
        "gap_s": r["gap_s"],
        "title": r["title"],
        "summary": r["summary"],
        "multi_speaker": r["multi_speaker"],
    }


def list_sessions(conn: sqlite3.Connection, from_ms: int | None = None, to_ms: int | None = None,
                  device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if from_ms is not None:
        where.append("end_utc_ms >= ?")
        params.append(from_ms)
    if to_ms is not None:
        where.append("start_utc_ms < ?")
        params.append(to_ms)
    if device_id:
        where.append("device_id = ?")
        params.append(device_id)
    params.append(max(1, min(int(limit), 1000)))
    rows = conn.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(where)} ORDER BY start_utc_ms DESC LIMIT ?",
        params,
    ).fetchall()
    return [_session_row(r) for r in rows]


def list_devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT device_id, count(*) AS n_chunks,
                  sum(status = 'pending') AS n_pending, sum(status = 'failed') AS n_failed,
                  min(start_utc_ms) AS first_ms, max(start_utc_ms) AS last_ms,
                  max(received_utc_ms) AS last_received_ms,
                  (SELECT count(*) FROM sessions s WHERE s.device_id = c.device_id) AS n_sessions
           FROM chunks c GROUP BY device_id ORDER BY device_id"""
    ).fetchall()
    return [
        {
            "device_id": r["device_id"],
            "n_chunks": r["n_chunks"],
            "n_pending": r["n_pending"],
            "n_failed": r["n_failed"],
            "n_sessions": r["n_sessions"],
            "first_chunk_utc": ms_to_iso(r["first_ms"]),
            "last_chunk_utc": ms_to_iso(r["last_ms"]),
            "last_received_utc": ms_to_iso(r["last_received_ms"]),
        }
        for r in rows
    ]


def session_segments(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT s.id, s.chunk_id, s.start_utc_ms, s.end_utc_ms, s.text, s.lang, s.model_id,
                  c.device_id, c.session_id
           FROM segments s JOIN chunks c ON c.id = s.chunk_id
           WHERE c.session_id = ?
           ORDER BY s.start_utc_ms, s.chunk_id, s.idx""",
        (session_id,),
    ).fetchall()
    return [_segment_row(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if r is None:
        return None
    d = _session_row(r)
    d["segments"] = session_segments(conn, session_id)
    return d


def transcript_lines(segments: list[dict[str, Any]]) -> list[str]:
    return [f"[{ms_to_hms(s['start_utc_ms'])}] {s['text']}" for s in segments]


def middle_truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n[... truncated ...]\n"
    keep = max(0, max_chars - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def get_segment_context(conn: sqlite3.Connection, segment_id: int, window_s: float = 60.0) -> dict[str, Any] | None:
    r = conn.execute(
        """SELECT s.id, s.chunk_id, s.start_utc_ms, s.end_utc_ms, s.text, s.lang, s.model_id,
                  c.device_id, c.session_id
           FROM segments s JOIN chunks c ON c.id = s.chunk_id WHERE s.id = ?""",
        (segment_id,),
    ).fetchone()
    if r is None:
        return None
    w = int(window_s * 1000)
    rows = conn.execute(
        """SELECT s.id, s.chunk_id, s.start_utc_ms, s.end_utc_ms, s.text, s.lang, s.model_id,
                  c.device_id, c.session_id
           FROM segments s JOIN chunks c ON c.id = s.chunk_id
           WHERE c.device_id = ? AND s.end_utc_ms >= ? AND s.start_utc_ms <= ?
           ORDER BY s.start_utc_ms, s.chunk_id, s.idx""",
        (r["device_id"], r["start_utc_ms"] - w, r["end_utc_ms"] + w),
    ).fetchall()
    return {
        "segment": _segment_row(r),
        "window_s": window_s,
        "context": [_segment_row(x) for x in rows],
        "text": "\n".join(transcript_lines([_segment_row(x) for x in rows])),
    }


def status(conn: sqlite3.Connection, archive_dir: Path | None = None, now: int | None = None) -> dict[str, Any]:
    now = now_ms() if now is None else now
    counts = {s: 0 for s in ("pending", "done", "failed")}
    for r in conn.execute("SELECT status, count(*) AS n FROM chunks GROUP BY status"):
        counts[r["status"]] = r["n"]
    oldest = conn.execute("SELECT min(received_utc_ms) AS t FROM chunks WHERE status = 'pending'").fetchone()["t"]
    last_done = conn.execute("SELECT max(transcribed_utc_ms) AS t FROM chunks").fetchone()["t"]
    archive_bytes = None
    if archive_dir is not None and archive_dir.exists():
        archive_bytes = sum(p.stat().st_size for p in archive_dir.rglob("*") if p.is_file())
    return {
        "now_utc": ms_to_iso(now),
        "chunks": counts,
        "segments": conn.execute("SELECT count(*) FROM segments").fetchone()[0],
        "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
        "open_sessions": conn.execute("SELECT count(*) FROM sessions WHERE closed = 0").fetchone()[0],
        "pending_oldest_age_s": round((now - oldest) / 1000, 1) if oldest else 0.0,
        "last_transcribed_utc": ms_to_iso(last_done) if last_done else None,
        "archive_bytes": archive_bytes,
        "devices": list_devices(conn),
    }
