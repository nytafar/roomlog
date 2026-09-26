"""Health check (design §3.7): status files + clock + spool → ``.prom`` file.

Runs from a timer. Unhealthy when the capture status file is stale or absent
(the unit is not running), the last frame is older than ``frame_age_max_s``,
the spool is above the high-water mark, or pending files have seen no
successful upload for ``upload_stall_s``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import clocksync
from .config import Config
from .spool import Spool
from .status import read_status


@dataclass
class Report:
    healthy: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def _age_s(written_utc_ns, now_ns: int) -> float | None:
    if not isinstance(written_utc_ns, (int, float)):
        return None
    return max(0.0, (now_ns - written_utc_ns) / 1e9)


def evaluate(cfg: Config, now_ns: int | None = None, ntp_state: bool | None = None,
             disk_usage=None) -> Report:
    now_ns = now_ns or time.time_ns()
    h = cfg.health
    reasons: list[str] = []
    m: dict[str, float] = {}

    cap = read_status(cfg.capture_status)
    cap_age = _age_s(cap.get("written_utc_ns"), now_ns) if cap else None
    if cap is None:
        reasons.append("capture status missing")
    elif cap.get("stopped"):
        reasons.append("capture stopped")
    elif cap_age is None or cap_age > h.status_stale_s:
        reasons.append(f"capture status stale ({cap_age:.0f} s)" if cap_age is not None else "capture status unstamped")
    frame_age = None
    if cap and cap_age is not None and isinstance(cap.get("last_frame_age_s"), (int, float)):
        frame_age = cap["last_frame_age_s"] + cap_age
        if frame_age > h.frame_age_max_s:
            reasons.append(f"last frame {frame_age:.0f} s ago")
    m["roomlog_last_frame_age_s"] = frame_age if frame_age is not None else float("nan")
    rms = cap.get("rms_dbfs") if cap else None
    m["roomlog_rms_dbfs"] = rms if isinstance(rms, (int, float)) else float("nan")
    m["roomlog_epoch"] = cap.get("epoch") if cap and isinstance(cap.get("epoch"), int) else float("nan")
    m["roomlog_chunks_dropped_total"] = cap.get("chunks_dropped", 0) if cap else 0

    spool = Spool(cfg.spool_dir, cfg.spool.max_bytes, cfg.spool.min_free_fraction)
    st = spool.stats()
    m["roomlog_spool_files"] = st.files
    m["roomlog_spool_bytes"] = st.total_bytes
    m["roomlog_failed_files"] = st.failed_files
    if not spool.disk_ok(disk_usage):
        reasons.append("spool above high-water mark or disk low")

    up = read_status(cfg.uploader_status)
    last_upload_age = _age_s(up.get("last_success_utc_ns"), now_ns) if up else None
    m["roomlog_last_upload_age_s"] = last_upload_age if last_upload_age is not None else float("nan")
    if st.pending_files:
        oldest = min(e.opus.stat().st_mtime for e in spool.entries("pending"))
        oldest_age = max(0.0, now_ns / 1e9 - oldest)
        if oldest_age > h.upload_stall_s and (last_upload_age is None or last_upload_age > h.upload_stall_s):
            reasons.append(f"pending files for {oldest_age:.0f} s without a successful upload")
    up_age = _age_s(up.get("written_utc_ns"), now_ns) if up else None
    if up is None or up_age is None or up_age > cfg.uploader.max_backoff_s + 60:
        reasons.append("uploader status missing or stale")
    if up and up.get("last_error") and isinstance(up.get("last_error"), str) and up["last_error"].startswith(("401", "403")):
        reasons.append(f"upload auth failure: {up['last_error']}")

    synced = clocksync.ntp_synchronized() if ntp_state is None else ntp_state
    m["roomlog_clock_synced"] = 1 if synced else 0
    m["roomlog_healthy"] = 0 if reasons else 1
    return Report(not reasons, reasons, m)


def render_prom(metrics: dict[str, float]) -> str:
    lines = []
    for k, v in metrics.items():
        if isinstance(v, float) and v != v:
            lines.append(f"{k} NaN")
        else:
            lines.append(f"{k} {v}")
    return "\n".join(lines) + "\n"


def write_prom(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def ping_healthchecks(url: str, healthy: bool, timeout_s: float = 10.0) -> bool:
    import http.client
    from urllib.parse import urlsplit

    target = url if healthy else url.rstrip("/") + "/fail"
    u = urlsplit(target)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    try:
        conn = cls(u.netloc, timeout=timeout_s)
        conn.request("GET", u.path or "/")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return 200 <= resp.status < 300
    except OSError:
        return False


def run_health(cfg: Config) -> int:
    report = evaluate(cfg)
    write_prom(cfg.metrics_file, render_prom(report.metrics))
    if cfg.health.healthchecks_url:
        ping_healthchecks(cfg.health.healthchecks_url, report.healthy)
    for r in report.reasons:
        print(f"unhealthy: {r}")
    if report.healthy:
        print("healthy")
    return 0 if report.healthy else 1
