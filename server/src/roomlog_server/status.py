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
    raw = st.get("raw")
    if raw is not None:
        lines += ["# HELP roomlog_raw_segments_total Raw segments in the archive by status.",
                  "# TYPE roomlog_raw_segments_total gauge"]
        for status in ("pending", "segmented", "failed"):
            lines.append(f'roomlog_raw_segments_total{{status="{status}"}} {raw[status]}')
        lines += ["# TYPE roomlog_raw_pending_oldest_age_seconds gauge",
                  f"roomlog_raw_pending_oldest_age_seconds {raw['pending_oldest_age_s']}"]
        if raw.get("bytes") is not None:
            lines += ["# TYPE roomlog_raw_bytes gauge", f"roomlog_raw_bytes {raw['bytes']}"]
    spans = st.get("spans")
    if spans is not None:
        lines += ["# HELP roomlog_dictation_spans_total Dictation spans received by worker status (ADR 0008).",
                  "# TYPE roomlog_dictation_spans_total gauge"]
        for status in ("pending", "applied"):
            lines.append(f'roomlog_dictation_spans_total{{status="{status}"}} {spans[status]}')
        lines += ["# TYPE roomlog_segments_superseded_total gauge",
                  f"roomlog_segments_superseded_total {spans['superseded_segments']}"]
    last = st.get("last_transcribed_utc")
    lines += [
        "# TYPE roomlog_last_transcribed_utc_seconds gauge",
        f"roomlog_last_transcribed_utc_seconds {(parse_user_time(last) or 0) / 1000 if last else 0}",
        "# TYPE roomlog_device_last_chunk_utc_seconds gauge",
    ]
    for d in st["devices"]:
        if d.get("last_chunk_utc"):
            lines.append(
                f'roomlog_device_last_chunk_utc_seconds{{device_id="{_label(d["device_id"])}"}} '
                f"{parse_user_time(d['last_chunk_utc']) / 1000}"
            )
    lines.append("# TYPE roomlog_device_chunks_pending gauge")
    for d in st["devices"]:
        lines.append(f'roomlog_device_chunks_pending{{device_id="{_label(d["device_id"])}"}} {d["n_pending"]}')
    if any(d.get("n_raw") for d in st["devices"]):
        lines.append("# TYPE roomlog_device_raw_pending gauge")
        for d in st["devices"]:
            if d.get("n_raw"):
                lines.append(f'roomlog_device_raw_pending{{device_id="{_label(d["device_id"])}"}} {d["n_raw_pending"]}')
        lines.append("# TYPE roomlog_device_last_raw_utc_seconds gauge")
        for d in st["devices"]:
            if d.get("last_raw_utc"):
                lines.append(
                    f'roomlog_device_last_raw_utc_seconds{{device_id="{_label(d["device_id"])}"}} '
                    f"{parse_user_time(d['last_raw_utc']) / 1000}"
                )
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
    raw = st.get("raw")
    if raw is not None:
        out.append(f"raw            pending={raw['pending']} segmented={raw['segmented']} "
                   f"failed={raw.get('failed', 0)} derived={raw['derived_chunks']} "
                   f"oldest pending {raw['pending_oldest_age_s']} s")
    spans = st.get("spans")
    if spans is not None:
        out.append(f"spans          pending={spans['pending']} applied={spans['applied']} "
                   f"superseded_segments={spans['superseded_segments']}")
    if st.get("archive_bytes") is not None:
        line = f"archive        {st['archive_bytes'] / 1e6:.1f} MB"
        if raw is not None and raw.get("bytes") is not None:
            line += f" (raw {raw['bytes'] / 1e6:.1f} MB)"
        out.append(line)
    if st["devices"]:
        out.append("devices:")
        for d in st["devices"]:
            line = (f"  {d['device_id']:<16} chunks={d['n_chunks']} pending={d['n_pending']} "
                    f"failed={d['n_failed']} last={d['last_chunk_utc'] or '-'}")
            if d.get("n_raw"):
                line += f" raw={d['n_raw']} raw_pending={d['n_raw_pending']} last_raw={d['last_raw_utc']}"
            out.append(line)
    return "\n".join(out) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
