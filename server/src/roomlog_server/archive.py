"""Archive storage (§2.4, §4.2): `archive/YYYY/MM/DD/<start_utc compact>_<sha8>.opus` + `.json`.

Files are written temp + fsync + rename; the sidecar is stored byte-for-byte as received.
The `chunks` row is inserted after both files are durable. Recovery covers both halves of a
crash between the two: file-without-row (insert the row, report `exists`) and
row-without-file (rewrite the files, report `exists`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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


def _files_match(archive_dir: Path, relpath: str, sha256: str, meta: dict[str, Any]) -> bool:
    opus = archive_dir / relpath
    try:
        return (hashlib.sha256(opus.read_bytes()).hexdigest() == sha256
                and json.loads(sidecar_path(opus).read_bytes()) == meta)
    except (OSError, ValueError):
        return False


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
    # Keep the exact header text in SQLite as well as in the archive. This
    # makes a missing sidecar recoverable byte-for-byte from the DB row.
    meta_json = meta_raw.decode("utf-8")
    # Serialize the file and row decision across handler threads and processes.
    # Unique temp names alone do not prevent two revised sidecars for the same
    # audio from choosing different final paths or overwriting each other.
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT path, meta_json FROM chunks WHERE sha256 = ?", (sha256,)).fetchone()
        if row is not None:
            relpath = row["path"]
            stored_meta = json.loads(row["meta_json"])
            if not _files_match(archive_dir, relpath, sha256, stored_meta):
                # A missing or corrupt archive must be repaired from the good
                # incoming audio. The DB's first sidecar remains authoritative.
                opus = archive_dir / relpath
                _write_durable(opus, body)
                # Rows written before raw JSON was retained have normalized
                # meta_json. A retry of the same metadata restores its received
                # bytes; a revised sidecar cannot replace the stored metadata.
                raw = meta_raw if meta == stored_meta else row["meta_json"].encode("utf-8")
                _write_durable(sidecar_path(opus), raw)
            result = StoreResult(created=False, sha256=sha256, path=relpath)
        else:
            relpath = archive_relpath(start_ms, sha256)
            opus = archive_dir / relpath
            existed = _files_present(archive_dir, relpath)
            if not _files_match(archive_dir, relpath, sha256, meta):
                _write_durable(opus, body)
                _write_durable(sidecar_path(opus), meta_raw)
            insert_chunk_row(conn, meta, meta_json, sha256, relpath)
            result = StoreResult(created=not existed, sha256=sha256, path=relpath)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


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
            meta_raw = json_path.read_bytes()
            meta = validate_sidecar(json.loads(meta_raw))
        except (ValueError, SidecarError) as e:
            log.warning("skipping %s: sidecar unusable (%s)", relpath, e)
            continue
        sha256 = hashlib.sha256(opus.read_bytes()).hexdigest()
        if meta["sha256"] != sha256 or relpath != archive_relpath(iso_to_ms(meta["start_utc"]), sha256):
            log.warning("skipping %s: sidecar, filename and audio hash disagree", relpath)
            continue
        if conn.execute("SELECT id FROM chunks WHERE sha256 = ?", (sha256,)).fetchone():
            continue
        meta_json = meta_raw.decode("utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            insert_chunk_row(conn, meta, meta_json, sha256, relpath)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        recovered.append(relpath)
    return recovered
