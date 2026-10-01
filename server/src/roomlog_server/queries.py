"""Read-only queries shared by the CLI and the MCP tools (§4.6). No raw SQL leaves this file.

Default filter set (ADR 0008): rows superseded by a dictation span are never listed, and
rows on the dictation channel are left out unless the caller asks for `include_dictation`.
Every row reports its `lang` and `channel` (the `channel` tag, `ambient` when untagged).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import fts_query
from .spans import CHANNEL_DEFAULT, CHANNEL_DICTATION, span_json
from .times import ms_to_hms, ms_to_iso, now_ms

_CHANNEL = f"""COALESCE((SELECT t.value FROM tags t WHERE t.target = 'segment' AND t.segment_id = s.id
                      AND t.key = 'channel' ORDER BY t.id DESC LIMIT 1), '{CHANNEL_DEFAULT}')"""
_SEG_COLS = f"""s.id, s.chunk_id, s.start_utc_ms, s.end_utc_ms, s.text, s.lang, s.model_id,
                s.device_id, s.span_id, c.session_id, {_CHANNEL} AS channel"""
_SEG_FROM = "LEFT JOIN chunks c ON c.id = s.chunk_id"
_NOT_DICTATION = f"""NOT EXISTS (SELECT 1 FROM tags t WHERE t.target = 'segment' AND t.segment_id = s.id
                                 AND t.key = 'channel' AND t.value = '{CHANNEL_DICTATION}')"""


def _default_where(include_dictation: bool) -> list[str]:
    where = ["s.superseded_by IS NULL"]
    if not include_dictation:
        where.append(_NOT_DICTATION)
    return where


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
        "channel": r["channel"],
        "model_id": r["model_id"],
    }
    if r["span_id"] is not None:
        d["span_id"] = r["span_id"]
    if score is not None:
        d["score"] = score
    return d


def search(conn: sqlite3.Connection, query: str, from_ms: int | None = None, to_ms: int | None = None,
           device_id: str | None = None, limit: int = 10, offset: int = 0,
           fuzzy: bool = False, include_dictation: bool = False) -> list[dict[str, Any]]:
    """bm25 over `segments_fts`, or trigram substring match over `segments_tri` with `fuzzy`.

    The trigram index cannot see terms shorter than three characters, so with `fuzzy` those
    terms (`ok`, `må`) are applied as case-folded substring tests on the candidate rows
    instead of being dropped; a query of only short terms scans without the index.
    Dictation rows are left out unless `include_dictation`.
    """
    terms = query.split()
    if not terms:
        return []
    params: list[Any] = []
    if fuzzy:
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        if long_terms:
            table = "segments_tri"
            where = ["segments_tri MATCH ?"]
            params.append(fts_query(" ".join(long_terms)))
        else:
            table = None
            where = []
        for t in short_terms:
            where.append("casefold_contains(s.text, ?)")
            params.append(t)
    else:
        table = "segments_fts"
        where = ["segments_fts MATCH ?"]
        params.append(fts_query(query))
    if from_ms is not None:
        where.append("s.start_utc_ms >= ?")
        params.append(from_ms)
    if to_ms is not None:
        where.append("s.start_utc_ms < ?")
        params.append(to_ms)
    if device_id:
        where.append("s.device_id = ?")
        params.append(device_id)
    where += _default_where(include_dictation)
    if table is not None:
        source = f"FROM {table} f JOIN segments s ON s.id = f.rowid"
        score = f"bm25({table})"
        order = "score, s.start_utc_ms"
    else:  # only short terms: no index can help, order newest first
        source = "FROM segments s"
        score = "NULL"
        order = "s.start_utc_ms DESC"
    sql = f"""
        SELECT {_SEG_COLS}, {score} AS score
        {source}
        {_SEG_FROM}
        WHERE {" AND ".join(where)}
        ORDER BY {order}
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
    """Per device: chunk counts and, for raw clients, the raw backlog and last raw segment.

    A device that has only sent raw segments so far (nothing segmented yet) is listed too,
    with `n_chunks` 0 and `last_chunk_utc` None.
    """
    rows = conn.execute(
        """SELECT device_id, count(*) AS n_chunks,
                  sum(status = 'pending') AS n_pending, sum(status = 'failed') AS n_failed,
                  sum(kind = 'derived') AS n_derived,
                  min(start_utc_ms) AS first_ms, max(start_utc_ms) AS last_ms,
                  max(received_utc_ms) AS last_received_ms,
                  (SELECT count(*) FROM sessions s WHERE s.device_id = c.device_id) AS n_sessions
           FROM chunks c GROUP BY device_id"""
    ).fetchall()
    devices: dict[str, dict[str, Any]] = {}
    for r in rows:
        devices[r["device_id"]] = {
            "device_id": r["device_id"],
            "n_chunks": r["n_chunks"],
            "n_pending": r["n_pending"],
            "n_failed": r["n_failed"],
            "n_derived": r["n_derived"],
            "n_sessions": r["n_sessions"],
            "first_chunk_utc": ms_to_iso(r["first_ms"]),
            "last_chunk_utc": ms_to_iso(r["last_ms"]),
            "last_received_utc": ms_to_iso(r["last_received_ms"]),
            "n_raw": 0,
            "n_raw_pending": 0,
            "last_raw_utc": None,
        }
    raw = conn.execute(
        """SELECT device_id, count(*) AS n_raw, sum(status = 'pending') AS n_raw_pending,
                  max(start_utc_ms) AS last_ms
           FROM raw_segments GROUP BY device_id"""
    ).fetchall()
    for r in raw:
        d = devices.setdefault(r["device_id"], {
            "device_id": r["device_id"], "n_chunks": 0, "n_pending": 0, "n_failed": 0, "n_derived": 0,
            "n_sessions": 0, "first_chunk_utc": None, "last_chunk_utc": None, "last_received_utc": None,
        })
        d["n_raw"] = r["n_raw"]
        d["n_raw_pending"] = r["n_raw_pending"]
        d["last_raw_utc"] = ms_to_iso(r["last_ms"])
    return [devices[k] for k in sorted(devices)]


def session_segments(conn: sqlite3.Connection, session_id: str,
                     include_dictation: bool = False) -> list[dict[str, Any]]:
    where = ["c.session_id = ?"] + _default_where(include_dictation)
    rows = conn.execute(
        f"""SELECT {_SEG_COLS} FROM segments s {_SEG_FROM}
            WHERE {" AND ".join(where)}
            ORDER BY s.start_utc_ms, s.chunk_id, s.idx""",
        (session_id,),
    ).fetchall()
    return [_segment_row(r) for r in rows]


def get_session(conn: sqlite3.Connection, session_id: str,
                include_dictation: bool = False) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if r is None:
        return None
    d = _session_row(r)
    d["segments"] = session_segments(conn, session_id, include_dictation)
    return d


def transcript_lines(segments: list[dict[str, Any]]) -> list[str]:
    """`[HH:MM:SS] text`; a row off the ambient channel says so: `[HH:MM:SS] (dictation en) text`."""
    out = []
    for s in segments:
        mark = "" if s.get("channel", CHANNEL_DEFAULT) == CHANNEL_DEFAULT else f"({s['channel']} {s['lang']}) "
        out.append(f"[{ms_to_hms(s['start_utc_ms'])}] {mark}{s['text']}")
    return out


def middle_truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n[... truncated ...]\n"
    keep = max(0, max_chars - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def get_segment_context(conn: sqlite3.Connection, segment_id: int, window_s: float = 60.0,
                        include_dictation: bool = False) -> dict[str, Any] | None:
    """The rows around one row (same device). The anchor is returned whatever its channel;
    the context applies the default filters unless `include_dictation`."""
    r = conn.execute(
        f"SELECT {_SEG_COLS} FROM segments s {_SEG_FROM} WHERE s.id = ?", (segment_id,),
    ).fetchone()
    if r is None:
        return None
    w = int(window_s * 1000)
    where = ["s.device_id = ?", "s.end_utc_ms >= ?", "s.start_utc_ms <= ?"] + _default_where(include_dictation)
    rows = conn.execute(
        f"""SELECT {_SEG_COLS} FROM segments s {_SEG_FROM}
            WHERE {" AND ".join(where)}
            ORDER BY s.start_utc_ms, s.chunk_id, s.idx""",
        (r["device_id"], r["start_utc_ms"] - w, r["end_utc_ms"] + w),
    ).fetchall()
    return {
        "segment": _segment_row(r),
        "window_s": window_s,
        "context": [_segment_row(x) for x in rows],
        "text": "\n".join(transcript_lines([_segment_row(x) for x in rows])),
    }


def list_spans(conn: sqlite3.Connection, from_ms: int | None = None, to_ms: int | None = None,
               device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Dictation spans as received, newest first, with their worker status."""
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
        f"SELECT * FROM dictation_spans WHERE {' AND '.join(where)} ORDER BY start_utc_ms DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = span_json(r)
        d["start_utc"] = ms_to_iso(r["start_utc_ms"])
        d["end_utc"] = ms_to_iso(r["end_utc_ms"])
        d["cancelled"] = bool(r["cancelled"])
        out.append(d)
    return out


def status(conn: sqlite3.Connection, archive_dir: Path | None = None, now: int | None = None) -> dict[str, Any]:
    now = now_ms() if now is None else now
    counts = {s: 0 for s in ("pending", "done", "failed")}
    for r in conn.execute("SELECT status, count(*) AS n FROM chunks GROUP BY status"):
        counts[r["status"]] = r["n"]
    oldest = conn.execute("SELECT min(received_utc_ms) AS t FROM chunks WHERE status = 'pending'").fetchone()["t"]
    last_done = conn.execute("SELECT max(transcribed_utc_ms) AS t FROM chunks").fetchone()["t"]
    raw_counts = {s: 0 for s in ("pending", "segmented", "failed")}
    for r in conn.execute("SELECT status, count(*) AS n FROM raw_segments GROUP BY status"):
        raw_counts[r["status"]] = r["n"]
    raw_oldest = conn.execute(
        "SELECT min(received_utc_ms) AS t FROM raw_segments WHERE status = 'pending'").fetchone()["t"]
    archive_bytes = None
    raw_bytes = None
    if archive_dir is not None and archive_dir.exists():
        archive_bytes = sum(p.stat().st_size for p in archive_dir.rglob("*") if p.is_file())
        raw_dir = archive_dir / "raw"
        raw_bytes = sum(p.stat().st_size for p in raw_dir.rglob("*") if p.is_file()) if raw_dir.exists() else 0
    return {
        "now_utc": ms_to_iso(now),
        "chunks": counts,
        "raw": {
            **raw_counts,
            "pending_oldest_age_s": round((now - raw_oldest) / 1000, 1) if raw_oldest else 0.0,
            "derived_chunks": conn.execute("SELECT count(*) FROM chunks WHERE kind = 'derived'").fetchone()[0],
            "bytes": raw_bytes,
        },
        "segments": conn.execute("SELECT count(*) FROM segments").fetchone()[0],
        "spans": {
            "pending": conn.execute("SELECT count(*) FROM dictation_spans WHERE status = 'pending'").fetchone()[0],
            "applied": conn.execute("SELECT count(*) FROM dictation_spans WHERE status = 'applied'").fetchone()[0],
            "superseded_segments": conn.execute(
                "SELECT count(*) FROM segments WHERE superseded_by IS NOT NULL").fetchone()[0],
        },
        "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
        "open_sessions": conn.execute("SELECT count(*) FROM sessions WHERE closed = 0").fetchone()[0],
        "pending_oldest_age_s": round((now - oldest) / 1000, 1) if oldest else 0.0,
        "last_transcribed_utc": ms_to_iso(last_done) if last_done else None,
        "archive_bytes": archive_bytes,
        "devices": list_devices(conn),
    }
