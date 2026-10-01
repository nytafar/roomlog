"""Dictation spans and tags (ADR 0008).

A span is what the dictation tool on a device reports after one dictation: start, end, the
text it produced, the language and engine, the mode, the target app, and whether it was
cancelled or empty. `POST /v1/spans` validates and stores it here; the worker applies it
(`worker.apply_spans`). Tags are facts on a transcript row or on a device time span, each
with a `source` (`deterministic`: a tool we control said so; `model`: inferred later) and an
`origin` naming the tool or model that produced it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .times import iso_to_ms, now_ms

MODES = ("raw", "cleanup", "edit-instruction")
MAX_TEXT_CHARS = 20_000
MAX_SPAN_MS = 3_600_000  # one hour: longer is a client bug, not a dictation
MAX_CANCELLED_MS = 600_000  # a cancelled span is clamped to ten minutes: its end was guessed
CHANNEL_DICTATION = "dictation"
CHANNEL_DEFAULT = "ambient"
TARGET_LIMITS = {"app": 200, "window": 500, "workspace": 200, "pane": 200,
                 "program": 200, "session_id": 200}


def target_facts(target):
    return {key: value for key, limit in TARGET_LIMITS.items()
            if (value := _str(target, key, required=False, max_len=limit))}


class SpanError(ValueError):
    pass


def _str(body: dict[str, Any], key: str, required: bool = True, max_len: int = 200) -> str | None:
    v = body.get(key)
    if v is None:
        if required:
            raise SpanError(f"{key} is required")
        return None
    if not isinstance(v, str):
        raise SpanError(f"{key} must be a string")
    if required and not v.strip():
        raise SpanError(f"{key} must not be empty")
    if len(v) > max_len:
        raise SpanError(f"{key} longer than {max_len} characters")
    return v


def validate_span(body: Any) -> dict[str, Any]:
    """Check a `POST /v1/spans` body and return it normalized (times in epoch ms)."""
    if not isinstance(body, dict):
        raise SpanError("body must be a JSON object")
    try:
        start_ms = iso_to_ms(_str(body, "start_utc", max_len=40))
        end_ms = iso_to_ms(_str(body, "end_utc", max_len=40))
    except ValueError as e:
        raise SpanError(str(e)) from None
    if end_ms < start_ms:
        raise SpanError("end_utc before start_utc")
    if end_ms - start_ms > MAX_SPAN_MS:
        raise SpanError("span longer than one hour")
    text = body.get("text", "")
    if not isinstance(text, str):
        raise SpanError("text must be a string")
    if len(text) > MAX_TEXT_CHARS:
        raise SpanError(f"text longer than {MAX_TEXT_CHARS} characters")
    lang = _str(body, "lang", max_len=16)
    assert lang is not None
    lang = lang.strip().lower()
    if not lang.replace("-", "").isalpha():
        raise SpanError("lang must be a language code")
    engine = _str(body, "engine")
    mode = _str(body, "mode", max_len=32)
    if mode not in MODES:
        raise SpanError(f"mode must be one of {', '.join(MODES)}")
    origin = _str(body, "origin")
    cancelled = body.get("cancelled", False)
    if not isinstance(cancelled, bool):
        raise SpanError("cancelled must be a boolean")
    target = body.get("target") or {}
    if not isinstance(target, dict):
        raise SpanError("target must be an object")
    facts = target_facts(target)
    app = facts.get("app")
    window = facts.get("window")
    device_id = _str(body, "device_id", required=False, max_len=64)
    # an empty dictation is a cancelled one: nothing was acted on (ADR 0008)
    cancelled = bool(cancelled or not text.strip())
    if cancelled and end_ms - start_ms > MAX_CANCELLED_MS:
        # the client never saw this one end and guessed; an absurd guess would hide
        # ambient speech under the dictation channel
        end_ms = start_ms + MAX_CANCELLED_MS
    return {
        "target": facts,
        "device_id": device_id,
        "start_utc_ms": start_ms,
        "end_utc_ms": end_ms,
        "text": text,
        "lang": lang,
        "engine": engine,
        "mode": mode,
        "app": app,
        "window": window,
        "cancelled": cancelled,
        "origin": origin,
    }


def add_tag(conn: sqlite3.Connection, key: str, value: str, origin: str, *, source: str = "deterministic",
            segment_id: int | None = None, device_id: str | None = None,
            start_utc_ms: int | None = None, end_utc_ms: int | None = None,
            now: int | None = None) -> int:
    """One tag on a transcript row (`segment_id`) or a device time span (the other three)."""
    if segment_id is not None:
        target = "segment"
    elif device_id is not None and start_utc_ms is not None and end_utc_ms is not None:
        target = "span"
    else:
        raise ValueError("a tag needs a segment_id or a device_id with start and end")
    cur = conn.execute(
        """INSERT INTO tags (target, segment_id, device_id, start_utc_ms, end_utc_ms, key, value,
                             source, origin, created_utc_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (target, segment_id, device_id, start_utc_ms, end_utc_ms, key, value, source, origin,
         now_ms() if now is None else now),
    )
    return int(cur.lastrowid)


def span_tags(span: sqlite3.Row | dict[str, Any]) -> list[tuple[str, str]]:
    """The deterministic facts a span carries, as (key, value) pairs."""
    out = [("channel", CHANNEL_DICTATION), ("mode", span["mode"])]
    # body_json already persists the additive target fields, so no schema change is
    # needed. Revalidate the whitelist when the worker reads a stored body.
    if "body_json" in span.keys():
        facts = target_facts(json.loads(span["body_json"]).get("target") or {})
    else:
        facts = span.get("target", {})
    facts = {**facts, **{k: span[k] for k in ("app", "window") if span[k]}}
    out.extend(facts.items())
    if span["cancelled"]:
        out.append(("cancelled", "true"))
    return out


def store_span(conn: sqlite3.Connection, device_id: str, span: dict[str, Any], body_raw: str,
               now: int | None = None) -> tuple[int, bool]:
    """Insert a validated span for `device_id`. Returns (id, created); a span with the same
    device and start is the same dictation posted again (the client spools and retries)."""
    now = now_ms() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM dictation_spans WHERE device_id = ? AND start_utc_ms = ?",
                           (device_id, span["start_utc_ms"])).fetchone()
        if row is not None:
            conn.execute("COMMIT")
            return int(row["id"]), False
        cur = conn.execute(
            """INSERT INTO dictation_spans (device_id, start_utc_ms, end_utc_ms, text, lang, engine, mode,
                                            app, window, cancelled, origin, body_json, received_utc_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_id, span["start_utc_ms"], span["end_utc_ms"], span["text"], span["lang"],
             span["engine"], span["mode"], span["app"], span["window"], 1 if span["cancelled"] else 0,
             span["origin"], body_raw, now),
        )
        span_id = int(cur.lastrowid)
        for key, value in span_tags(span):
            add_tag(conn, key, value, span["origin"], device_id=device_id,
                    start_utc_ms=span["start_utc_ms"], end_utc_ms=span["end_utc_ms"], now=now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return span_id, True


def spans_overlapping(conn: sqlite3.Connection, device_id: str, start_utc_ms: int,
                      end_utc_ms: int) -> list[sqlite3.Row]:
    """Spans of `device_id` that overlap [start, end), in time order."""
    return conn.execute(
        """SELECT * FROM dictation_spans
           WHERE device_id = ? AND start_utc_ms < ? AND end_utc_ms > ?
           ORDER BY start_utc_ms, id""",
        (device_id, end_utc_ms, start_utc_ms),
    ).fetchall()


def span_json(span: sqlite3.Row) -> dict[str, Any]:
    return {k: span[k] for k in span.keys() if k != "body_json"}


def parse_body(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))
