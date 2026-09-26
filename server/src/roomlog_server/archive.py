"""Archive storage (§2.4, §4.2): `archive/YYYY/MM/DD/<start_utc compact>_<sha8>.opus` + `.json`.

Files are written temp + fsync + rename; the sidecar is stored byte-for-byte as received.
The `chunks` row is inserted after both files are durable. Recovery covers both halves of a
crash between the two: file-without-row (insert the row, report `exists`) and
row-without-file (rewrite the files, report `exists`).
"""

from __future__ import annotations

import hashlib
import json
import os
import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sidecar import SidecarError, validate_sidecar
from .times import iso_to_ms, ms_to_compact, now_ms

log = logging.getLogger("roomlog.archive")


@dataclass
class StoreResult:
    created: bool
    sha256: str
    path: str  # relative to the archive dir, `.opus`


def archive_relpath(start_utc_ms: int, sha256: str) -> str:
    compact = ms_to_compact(start_utc_ms)
    day = compact[:8]
    return f"{day[0:4]}/{day[4:6]}/{day[6:8]}/{compact}_{sha256[:8]}.opus"


def sidecar_path(opus_path: Path) -> Path:
    return opus_path.with_suffix(".json")


def _write_durable(path: Path, data: bytes) -> None:
    """Temp + fsync + rename. The temp name is unique per writer (`mkstemp`), so two
    concurrent PUTs of the same sha cannot truncate or unlink each other's file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _files_present(archive_dir: Path, relpath: str) -> bool:
    opus = archive_dir / relpath
    return opus.exists() and sidecar_path(opus).exists()


def insert_chunk_row(conn: sqlite3.Connection, meta: dict[str, Any], meta_json: str,
                     sha256: str, relpath: str, received_utc_ms: int | None = None) -> int:
    start_ms = iso_to_ms(meta["start_utc"])
    duration_ms = int(round(float(meta["duration_s"]) * 1000))
    cur = conn.execute(
        """INSERT INTO chunks (sha256, device_id, start_utc_ms, end_utc_ms, duration_ms, path,
                               meta_json, run_id, epoch, discontinuity, clock_synced,
                               received_utc_ms, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (
            sha256,
            meta["device_id"],
            start_ms,
            start_ms + duration_ms,
            duration_ms,
            relpath,
            meta_json,
            meta["run_id"],
            int(meta["epoch"]),
            1 if meta["discontinuity"] else 0,
            1 if meta["clock_synced"] else 0,
            received_utc_ms if received_utc_ms is not None else now_ms(),
        ),
    )
    return int(cur.lastrowid)


def store_chunk(conn: sqlite3.Connection, archive_dir: Path, meta: dict[str, Any],
                meta_raw: bytes, body: bytes) -> StoreResult:
    """Store one validated chunk. `meta_raw` is the sidecar exactly as received.

    The caller has already checked the token, the sha and the schema.
    """
    sha256 = hashlib.sha256(body).hexdigest()
    start_ms = iso_to_ms(meta["start_utc"])
    row = conn.execute("SELECT id, path FROM chunks WHERE sha256 = ?", (sha256,)).fetchone()
    if row is not None:
        relpath = row["path"]
        if not _files_present(archive_dir, relpath):
            # row-without-file: the archive lost the files (or a crash after rename of one)
            opus = archive_dir / relpath
            _write_durable(opus, body)
            _write_durable(sidecar_path(opus), meta_raw)
        return StoreResult(created=False, sha256=sha256, path=relpath)

    relpath = archive_relpath(start_ms, sha256)
    opus = archive_dir / relpath
    meta_json = json.dumps(meta, ensure_ascii=True, separators=(",", ":"))
    if _files_present(archive_dir, relpath):
        # file-without-row: crash between rename and insert
        conn.execute("BEGIN IMMEDIATE")
        try:
            again = conn.execute("SELECT id FROM chunks WHERE sha256 = ?", (sha256,)).fetchone()
            if again is None:
                insert_chunk_row(conn, meta, meta_json, sha256, relpath)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return StoreResult(created=False, sha256=sha256, path=relpath)

    _write_durable(opus, body)
    _write_durable(sidecar_path(opus), meta_raw)
    conn.execute("BEGIN IMMEDIATE")
    try:
        again = conn.execute("SELECT id FROM chunks WHERE sha256 = ?", (sha256,)).fetchone()
        if again is not None:
            conn.execute("COMMIT")
            return StoreResult(created=False, sha256=sha256, path=relpath)
        insert_chunk_row(conn, meta, meta_json, sha256, relpath)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return StoreResult(created=True, sha256=sha256, path=relpath)


def scan_orphans(conn: sqlite3.Connection, archive_dir: Path) -> list[str]:
    """Archive files with no `chunks` row: insert rows for them (startup recovery).

    Known paths are loaded once into a set (one indexed scan, not one query per file), and a
    sidecar that does not parse or validate is logged and skipped rather than blocking startup.
    """
    recovered: list[str] = []
    if not archive_dir.exists():
        return recovered
    known_paths = {r[0] for r in conn.execute("SELECT path FROM chunks")}
    for json_path in sorted(archive_dir.rglob("*.json")):
        opus = json_path.with_suffix(".opus")
        if not opus.exists():
            continue
        relpath = str(opus.relative_to(archive_dir))
        if relpath in known_paths:
            continue
        try:
            meta = validate_sidecar(json.loads(json_path.read_bytes()))
        except (ValueError, SidecarError) as e:
            log.warning("skipping %s: sidecar unusable (%s)", relpath, e)
            continue
        sha256 = hashlib.sha256(opus.read_bytes()).hexdigest()
        if conn.execute("SELECT id FROM chunks WHERE sha256 = ?", (sha256,)).fetchone():
            continue
        meta_json = json.dumps(meta, ensure_ascii=True, separators=(",", ":"))
        conn.execute("BEGIN IMMEDIATE")
        try:
            insert_chunk_row(conn, meta, meta_json, sha256, relpath)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        recovered.append(relpath)
    return recovered
