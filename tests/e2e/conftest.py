"""Run both standalone src-layout packages in the server test environment."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
for package in ("edge", "server"):
    src = str(ROOT / package / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
