"""`roomlog status [--prom]`: human summary or Prometheus text format (for the health timer)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .times import parse_user_time


def _label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prom(st: dict[str, Any]) -> str:
    lines = [
        "# HELP roomlog_chunks_total Chunks in the archive by status.",
        "# TYPE roomlog_chunks_total gauge",
    ]
    for status, n in st["chunks"].items():
        lines.append(f'roomlog_chunks_total{{status="{status}"}} {n}')
    lines += [
        "# TYPE roomlog_segments_total gauge",
        f"roomlog_segments_total {st['segments']}",
        "# TYPE roomlog_sessions_total gauge",
        f"roomlog_sessions_total {st['sessions']}",
        "# TYPE roomlog_sessions_open gauge",
        f"roomlog_sessions_open {st['open_sessions']}",
        "# TYPE roomlog_pending_oldest_age_seconds gauge",
        f"roomlog_pending_oldest_age_seconds {st['pending_oldest_age_s']}",
    ]
    if st.get("archive_bytes") is not None:
        lines += ["# TYPE roomlog_archive_bytes gauge", f"roomlog_archive_bytes {st['archive_bytes']}"]
    last = st.get("last_transcribed_utc")
    lines += [
        "# TYPE roomlog_last_transcribed_utc_seconds gauge",
        f"roomlog_last_transcribed_utc_seconds {(parse_user_time(last) or 0) / 1000 if last else 0}",
        "# TYPE roomlog_device_last_chunk_utc_seconds gauge",
    ]
    for d in st["devices"]:
        lines.append(
            f'roomlog_device_last_chunk_utc_seconds{{device_id="{_label(d["device_id"])}"}} '
            f"{parse_user_time(d['last_chunk_utc']) / 1000}"
        )
    lines.append("# TYPE roomlog_device_chunks_pending gauge")
    for d in st["devices"]:
        lines.append(f'roomlog_device_chunks_pending{{device_id="{_label(d["device_id"])}"}} {d["n_pending"]}')
    return "\n".join(lines) + "\n"


def render_text(st: dict[str, Any]) -> str:
    c = st["chunks"]
    out = [
        f"now            {st['now_utc']}",
        f"chunks         pending={c['pending']} done={c['done']} failed={c['failed']}",
        f"segments       {st['segments']}",
        f"sessions       {st['sessions']} ({st['open_sessions']} open)",
        f"oldest pending {st['pending_oldest_age_s']} s",
        f"last transcribed {st['last_transcribed_utc'] or '-'}",
    ]
    if st.get("archive_bytes") is not None:
        out.append(f"archive        {st['archive_bytes'] / 1e6:.1f} MB")
    if st["devices"]:
        out.append("devices:")
        for d in st["devices"]:
            out.append(
                f"  {d['device_id']:<16} chunks={d['n_chunks']} pending={d['n_pending']} "
                f"failed={d['n_failed']} last={d['last_chunk_utc']}"
            )
    return "\n".join(out) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
