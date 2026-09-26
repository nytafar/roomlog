"""UTC time helpers. Times in the DB are integer epoch milliseconds."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(s: str) -> int:
    """`2026-09-26T10:15:32.417Z` → epoch ms. Strict: this is the sidecar format."""
    if not _ISO_MS.match(s):
        raise ValueError(f"not a sidecar timestamp: {s!r}")
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ms_to_compact(ms: int) -> str:
    """`20260926T101532417Z`, the archive-name and session-id form."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S") + f"{dt.microsecond // 1000:03d}Z"


def ms_to_hms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S")


def parse_user_time(s: str | None) -> int | None:
    """Lenient parsing for CLI/MCP filters: date, ISO with or without Z/offset, or epoch ms."""
    if s is None or s == "":
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))
