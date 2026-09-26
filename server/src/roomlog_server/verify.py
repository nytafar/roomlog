"""`roomlog verify`: re-hash the archive against sidecars and cross-check with the DB."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def verify_archive(conn: sqlite3.Connection, archive_dir: Path) -> dict[str, Any]:
    problems: list[str] = []
    n_files = 0
    seen_paths: set[str] = set()
    if archive_dir.exists():
        for json_path in sorted(archive_dir.rglob("*.json")):
            opus = json_path.with_suffix(".opus")
            rel = str(opus.relative_to(archive_dir))
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
            row = conn.execute("SELECT path, sha256 FROM chunks WHERE sha256 = ?", (sha,)).fetchone()
            if row is None:
                problems.append(f"{rel}: no chunks row")
            elif row["path"] != rel:
                problems.append(f"{rel}: chunks row points at {row['path']}")
            seen_paths.add(rel)
        for opus in sorted(archive_dir.rglob("*.opus")):
            if not opus.with_suffix(".json").exists():
                problems.append(f"{opus.relative_to(archive_dir)}: audio without sidecar")
        for tmp in archive_dir.rglob("*.tmp"):
            problems.append(f"{tmp.relative_to(archive_dir)}: leftover temp file")
    n_rows = 0
    for r in conn.execute("SELECT path FROM chunks"):
        n_rows += 1
        if r["path"] not in seen_paths and not (archive_dir / r["path"]).exists():
            problems.append(f"{r['path']}: chunks row without file")
    return {"files": n_files, "rows": n_rows, "problems": problems, "ok": not problems}
