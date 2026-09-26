"""`roomlog export --vault DIR`: one markdown file per session with YAML front matter.

Idempotent: a file is rewritten only when its content would change.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .queries import get_session, list_sessions, transcript_lines
from .times import ms_to_iso


def _yaml_str(s: str | None) -> str:
    if s is None:
        return "null"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_session(session: dict) -> str:
    fm = [
        "---",
        "kind: roomlog-session",
        f"session_id: {_yaml_str(session['session_id'])}",
        f"device_id: {_yaml_str(session['device_id'])}",
        f"date: {session['start_utc'][:10]}",
        f"start_utc: {_yaml_str(session['start_utc'])}",
        f"end_utc: {_yaml_str(session['end_utc'])}",
        f"duration_s: {session['duration_s']}",
        f"n_chunks: {session['n_chunks']}",
        f"n_segments: {session['n_segments']}",
        f"closed: {'true' if session['closed'] else 'false'}",
        f"gap_s: {session['gap_s']}",
        f"title: {_yaml_str(session.get('title'))}",
        "---",
        "",
    ]
    title = session.get("title") or f"{session['device_id']} {session['start_utc']}"
    body = [f"# {title}", ""]
    body += transcript_lines(session["segments"])
    return "\n".join(fm + body) + "\n"


def export_sessions(conn: sqlite3.Connection, vault: Path, from_ms: int | None = None,
                    to_ms: int | None = None, device_id: str | None = None,
                    only_closed: bool = False) -> dict[str, int]:
    vault.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for s in list_sessions(conn, from_ms, to_ms, device_id, limit=1_000_000):
        if only_closed and not s["closed"]:
            continue
        full = get_session(conn, s["session_id"])
        assert full is not None
        content = render_session(full)
        target = vault / f"{s['session_id']}.md"
        if target.exists() and target.read_text(encoding="utf-8") == content:
            skipped += 1
            continue
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        written += 1
    return {"written": written, "unchanged": skipped}
