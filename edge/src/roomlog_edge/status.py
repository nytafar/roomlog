"""Live status files, written by temp+rename (design §3.1)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def write_status(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload.setdefault("written_utc_ns", time.time_ns())
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_status(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
