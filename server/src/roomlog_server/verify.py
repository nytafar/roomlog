"""`roomlog verify`: re-hash the archive against sidecars and cross-check with the DB.

Speech chunks live under `archive/YYYY/...` and map to `chunks`; raw segments live under
`archive/raw/...` and map to `raw_segments`. Derived chunks have no file; for them the check
is that their raw audio is still present and contiguous.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .archive import RAW_PREFIX
from .segmenter import covering_segments, raw_covers


def verify_archive(conn: sqlite3.Connection, archive_dir: Path) -> dict[str, Any]:
    problems: list[str] = []
    n_files = 0
    seen_paths: set[str] = set()
    if archive_dir.exists():
        for json_path in sorted(archive_dir.rglob("*.json")):
            opus = json_path.with_suffix(".opus")
            rel = str(opus.relative_to(archive_dir))
            table = "raw_segments" if rel.startswith(RAW_PREFIX) else "chunks"
            n_files += 1
            if not opus.exists():
                problems.append(f"{rel}: sidecar without audio")
                continue
            try:
                meta = json.loads(json_path.read_bytes())
            except ValueError:
                problems.append(f"{rel}: sidecar is not JSON")
                continue
            sha = hashlib.sha256(opus.read_bytes()).hexdigest()
            if meta.get("sha256") != sha:
                problems.append(f"{rel}: sha256 mismatch (file {sha[:8]}, sidecar {str(meta.get('sha256'))[:8]})")
            if not opus.stem.endswith(sha[:8]):
                problems.append(f"{rel}: filename does not carry sha8 {sha[:8]}")
            is_raw = meta.get("kind") == "raw"
            if is_raw != (table == "raw_segments"):
                problems.append(f"{rel}: kind {meta.get('kind', 'speech')!r} does not belong under this directory")
            row = conn.execute(f"SELECT path, sha256 FROM {table} WHERE sha256 = ?", (sha,)).fetchone()
            if row is None:
                problems.append(f"{rel}: no {table} row")
            elif row["path"] != rel:
                problems.append(f"{rel}: {table} row points at {row['path']}")
            seen_paths.add(rel)
        for opus in sorted(archive_dir.rglob("*.opus")):
            if not opus.with_suffix(".json").exists():
                problems.append(f"{opus.relative_to(archive_dir)}: audio without sidecar")
        for tmp in archive_dir.rglob("*.tmp"):
            problems.append(f"{tmp.relative_to(archive_dir)}: leftover temp file")
    n_rows = 0
    for r in conn.execute("SELECT path FROM chunks WHERE kind = 'speech'"):
        n_rows += 1
        if r["path"] not in seen_paths and not (archive_dir / r["path"]).exists():
            problems.append(f"{r['path']}: chunks row without file")
    for r in conn.execute("SELECT path FROM raw_segments"):
        n_rows += 1
        if r["path"] not in seen_paths and not (archive_dir / r["path"]).exists():
            problems.append(f"{r['path']}: raw_segments row without file")
    n_derived = 0
    for r in conn.execute(
            "SELECT id, device_id, run_id, epoch, n_start, n_samples FROM chunks WHERE kind = 'derived'"):
        n_derived += 1
        n_end = r["n_start"] + r["n_samples"]
        segs = covering_segments(conn, r["device_id"], r["run_id"], r["epoch"], r["n_start"], n_end)
        if not raw_covers(segs, r["n_start"], n_end):
            problems.append(f"derived chunk {r['id']} ({r['device_id']} n={r['n_start']}): raw audio missing")
    return {"files": n_files, "rows": n_rows, "derived": n_derived, "problems": problems, "ok": not problems}
