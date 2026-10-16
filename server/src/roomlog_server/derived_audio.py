"""Playable files for derived chunks (ADR 0005 note, 2026-10-16).

A derived chunk is a slice of the raw timeline. So the recall reader can serve it like a
speech chunk, the slice is also stored as its own Ogg Opus file holding exactly the chunk's
samples (16 kHz mono, time zero = the chunk's first sample) under
`archive/derived/YYYY/MM/DD/<start compact>_<sha8>.opus`, and `chunks.path` points at it.

`sha256` keeps its ADR 0005 meaning, the SHA-256 of the int16 LE PCM slice: it is the row's
identity (re-segmenting a span is idempotent through `UNIQUE(sha256)`), and the file is a
deterministic function of that PCM (`audio.encode_opus` is bit-exact), so the reader's ETag
still names one body. There is no sidecar: the row's `meta_json` is the metadata, and the
file can always be rebuilt from raw.

Files and rows stay in step through one rule: **a file is removed only under the database
write lock, and only when no `chunks` row references its path; a writer checks, under the
same lock, that the file exists right before it commits a row (or path) pointing at it.**
Writers encode and write outside the lock (CPU and fsync), so a sweep may remove a file
between that write and the lock; the in-lock check then writes it again. A crash between
write and commit leaves an unreferenced file: the next pass that derives the same chunk
reuses it (same PCM, same path), otherwise the sweep removes it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .archive import _write_durable, archive_relpath
from .audio import decode_opus, encode_opus

log = logging.getLogger("roomlog.derived_audio")

DERIVED_PREFIX = "derived/"
SWEEP_MIN_AGE_S = 3600.0  # a file this young may belong to a pass that has not committed yet


def derived_relpath(start_utc_ms: int, sha256: str) -> str:
    return DERIVED_PREFIX + archive_relpath(start_utc_ms, sha256)


def ensure_file(archive_dir: Path, relpath: str, body: Callable[[], bytes]) -> int:
    """Write the file unless it is there; returns the bytes written (0 when it existed).

    An existing file is never rewritten: `_write_durable` renames into place, so a file at
    the final path is complete, and leaving it keeps the bytes behind an ETag stable.
    """
    target = archive_dir / relpath
    if target.is_file():
        return 0
    data = body()
    _write_durable(target, data)
    return len(data)


def _referenced(conn: sqlite3.Connection, relpath: str) -> bool:
    return conn.execute("SELECT 1 FROM chunks WHERE path = ? LIMIT 1", (relpath,)).fetchone() is not None


def _unlink(archive_dir: Path, relpath: str) -> int | None:
    """Remove one file; its size, or None when it was not there.

    Empty day directories stay: a writer outside the lock may be between its `mkdir` and
    its temp file there, and one directory per day costs nothing.
    """
    target = archive_dir / relpath
    try:
        size = target.stat().st_size
        target.unlink()
    except FileNotFoundError:
        return None
    return size


def remove_unreferenced(conn: sqlite3.Connection, archive_dir: Path, relpaths: Iterable[str]) -> dict[str, int]:
    """Delete the given derived files that no `chunks` row references, under the write lock.

    Must be called outside a transaction. Paths outside `derived/` are ignored.
    """
    out = {"removed": 0, "bytes": 0}
    paths = sorted({p for p in relpaths if p and p.startswith(DERIVED_PREFIX)})
    if not paths:
        return out
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rel in paths:
            if _referenced(conn, rel):
                continue
            size = _unlink(archive_dir, rel)
            if size is not None:
                out["removed"] += 1
                out["bytes"] += size
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def sweep(conn: sqlite3.Connection, archive_dir: Path, min_age_s: float = SWEEP_MIN_AGE_S,
          now: Callable[[], float] = time.time) -> dict[str, int]:
    """Remove derived files no row references (crash leftovers, rows deleted without their
    files), and stale temp files, older than `min_age_s`."""
    out = {"removed": 0, "bytes": 0, "tmp_removed": 0}
    root = archive_dir / DERIVED_PREFIX
    if not root.exists():
        return out
    cutoff = now() - min_age_s
    known = {r[0] for r in conn.execute("SELECT path FROM chunks WHERE kind = 'derived' AND path IS NOT NULL")}
    candidates: list[str] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            if f.stat().st_mtime > cutoff:
                continue
        except FileNotFoundError:
            continue
        rel = str(f.relative_to(archive_dir))
        if f.suffix == ".tmp":
            # `_write_durable` removes its temp file unless the process died mid-write.
            try:
                f.unlink()
                out["tmp_removed"] += 1
            except FileNotFoundError:
                pass
        elif f.suffix == ".opus" and rel not in known:
            candidates.append(rel)
    res = remove_unreferenced(conn, archive_dir, candidates)
    out["removed"], out["bytes"] = res["removed"], res["bytes"]
    if out["removed"] or out["tmp_removed"]:
        log.info("swept %d unreferenced derived audio file(s) (%d bytes), %d temp file(s)",
                 out["removed"], out["bytes"], out["tmp_removed"])
    return out


# ---------------------------------------------------------------- backfill


@dataclass
class BackfillResult:
    derived: int = 0  # derived chunks seen
    present: int = 0  # already had a path and its file
    written: int = 0  # files written
    linked: int = 0  # rows that got their path (or had a missing file restored)
    bytes: int = 0  # bytes written
    raw_missing: int = 0  # raw audio no longer covers the chunk; left without a file
    mismatched: int = 0  # the raw slice does not hash to the row's sha256; left without a file
    gone: int = 0  # the row was deleted while its file was being written
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def backfill(conn: sqlite3.Connection, archive_dir: Path,
             decode: Callable[[str], np.ndarray] = decode_opus,
             limit: int | None = None,
             progress: Callable[[BackfillResult], None] | None = None) -> BackfillResult:
    """Write the file of every derived chunk that lacks one, from its raw segments.

    Idempotent and resumable: each chunk is committed on its own, and a chunk whose path is
    set and whose file exists is skipped. Safe beside a running worker: the row is re-read
    under the write lock before its path is set, and a chunk deleted meanwhile has its file
    removed again. The slice must hash to the row's `sha256`, so the file holds exactly the
    samples the segmenter derived; a chunk whose raw no longer gives them is reported and
    left without a file.
    """
    from .segmenter import RawAudio, covering_segments, pcm_int16, pcm_sha256, raw_covers

    res = BackfillResult()
    raw = RawAudio(archive_dir, decode)
    rows = conn.execute(
        """SELECT id, sha256, device_id, run_id, epoch, n_start, n_samples, start_utc_ms, path
           FROM chunks WHERE kind = 'derived' ORDER BY device_id, run_id, epoch, n_start"""
    ).fetchall()
    todo = 0
    for r in rows:
        res.derived += 1
        if r["path"] and (archive_dir / r["path"]).is_file():
            res.present += 1
            continue
        if limit is not None and todo >= limit:
            continue
        todo += 1
        n_end = r["n_start"] + r["n_samples"]
        try:
            segs = covering_segments(conn, r["device_id"], r["run_id"], r["epoch"], r["n_start"], n_end)
            if not raw_covers(segs, r["n_start"], n_end):
                res.raw_missing += 1
                continue
            audio = raw.span(segs, r["n_start"], n_end)
            raw.keep_only(segs)  # chunks are in timeline order: the next one shares these
            if pcm_sha256(audio) != r["sha256"]:
                log.warning("derived chunk %d: raw slice does not hash to its sha256; skipped", r["id"])
                res.mismatched += 1
                continue
            pcm = pcm_int16(audio)
            rel = r["path"] or derived_relpath(r["start_utc_ms"], r["sha256"])
            written = ensure_file(archive_dir, rel, lambda: encode_opus(pcm))
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("SELECT path, sha256 FROM chunks WHERE id = ? AND kind = 'derived'",
                                   (r["id"],)).fetchone()
                if cur is None or cur["sha256"] != r["sha256"] or (cur["path"] and cur["path"] != rel):
                    stale = True
                else:
                    stale = False
                    written += ensure_file(archive_dir, rel, lambda: encode_opus(pcm))
                    if cur["path"] is None:
                        conn.execute("UPDATE chunks SET path = ? WHERE id = ? AND path IS NULL", (rel, r["id"]))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            res.bytes += written
            res.written += 1 if written else 0
            if stale:
                res.gone += 1
                remove_unreferenced(conn, archive_dir, [rel])
            else:
                res.linked += 1
        except Exception as e:  # one bad chunk must not stop the rest
            log.exception("backfilling derived chunk %d failed", r["id"])
            res.errors.append(f"chunk {r['id']}: {e.__class__.__name__}: {e}")
        if progress is not None:
            progress(res)
    raw.keep_only([])
    return res


__all__ = ["DERIVED_PREFIX", "BackfillResult", "backfill", "derived_relpath", "ensure_file",
           "remove_unreferenced", "sweep"]
