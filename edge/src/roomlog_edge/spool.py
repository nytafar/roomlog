"""Spool directory (design §3.5, §3.3 unsynced hold).

Layout under ``spool_dir``: ``tmp/`` (in-progress writes), ``pending/`` (ready
to upload), ``unsynced/`` (held until the clock syncs), ``failed/`` (server
rejected for good). A chunk is ``<start_utc compact>_<sha8>.opus`` plus the
same stem ``.json``; lexical order is upload order.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import sidecar

DIRS = ("tmp", "pending", "unsynced", "failed")


@dataclass(frozen=True)
class Entry:
    stem: str
    opus: Path
    json: Path

    @property
    def size(self) -> int:
        return self.opus.stat().st_size

    def read_meta(self) -> dict:
        with open(self.json, "rb") as f:
            return json.load(f)


@dataclass(frozen=True)
class Stats:
    pending_files: int
    pending_bytes: int
    unsynced_files: int
    failed_files: int
    total_bytes: int

    @property
    def files(self) -> int:
        return self.pending_files + self.unsynced_files


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsync(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


class Spool:
    def __init__(self, root: Path, max_bytes: int = 2 * 1024**3, min_free_fraction: float = 0.05):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.min_free_fraction = min_free_fraction
        for d in DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def dir(self, name: str) -> Path:
        return self.root / name

    # -- writing -----------------------------------------------------------

    def cleanup_tmp(self) -> int:
        n = 0
        for p in self.dir("tmp").iterdir():
            if p.is_file():
                p.unlink()
                n += 1
        return n

    @staticmethod
    def stem_for(meta: dict) -> str:
        return f"{sidecar.compact_utc(meta['start_utc'])}_{meta['sha256'][:8]}"

    def write(self, opus: bytes, meta: dict, dest: str = "pending") -> Entry:
        """Write ``.opus`` and ``.json`` via ``tmp/`` and rename into ``dest``.

        ``meta['sha256']`` is set from the bytes here; the caller need not.
        """
        assert dest in ("pending", "unsynced")
        meta = dict(meta)
        meta["sha256"] = sha256_hex(opus)
        stem = self.stem_for(meta)
        tmp = self.dir("tmp")
        t_opus, t_json = tmp / f"{stem}.opus.tmp", tmp / f"{stem}.json.tmp"
        _write_fsync(t_opus, opus)
        _write_fsync(t_json, sidecar.dumps_file(meta).encode())
        target = self.dir(dest)
        f_opus, f_json = target / f"{stem}.opus", target / f"{stem}.json"
        os.replace(t_opus, f_opus)
        os.replace(t_json, f_json)
        _fsync_dir(target)
        return Entry(stem, f_opus, f_json)

    def rewrite_meta(self, entry: Entry, meta: dict, dest: str) -> Entry:
        """Replace the sidecar (start_utc may change, so the stem may too) and
        move both files into ``dest``."""
        stem = self.stem_for(meta)
        tmp_json = self.dir("tmp") / f"{stem}.json.tmp"
        _write_fsync(tmp_json, sidecar.dumps_file(meta).encode())
        target = self.dir(dest)
        f_opus, f_json = target / f"{stem}.opus", target / f"{stem}.json"
        os.replace(entry.opus, f_opus)
        os.replace(tmp_json, f_json)
        if entry.json != f_json:
            entry.json.unlink(missing_ok=True)
        _fsync_dir(target)
        return Entry(stem, f_opus, f_json)

    # -- listing -----------------------------------------------------------

    def entries(self, name: str = "pending") -> list[Entry]:
        d = self.dir(name)
        out = []
        for opus in sorted(d.glob("*.opus")):
            js = opus.with_suffix(".json")
            if js.exists():
                out.append(Entry(opus.stem, opus, js))
        return out

    def move(self, entry: Entry, dest: str) -> Entry:
        target = self.dir(dest)
        f_opus, f_json = target / entry.opus.name, target / entry.json.name
        os.replace(entry.opus, f_opus)
        os.replace(entry.json, f_json)
        return Entry(entry.stem, f_opus, f_json)

    def delete(self, entry: Entry) -> None:
        entry.opus.unlink(missing_ok=True)
        entry.json.unlink(missing_ok=True)

    def stats(self) -> Stats:
        def count(name):
            files = bytes_ = 0
            for p in self.dir(name).iterdir():
                if p.is_file():
                    if p.suffix == ".opus":
                        files += 1
                    bytes_ += p.stat().st_size
            return files, bytes_

        pf, pb = count("pending")
        uf, ub = count("unsynced")
        ff, fb = count("failed")
        _, tb = count("tmp")
        return Stats(pf, pb, uf, ff, pb + ub + fb + tb)

    # -- disk guard --------------------------------------------------------

    def disk_ok(self, usage=None) -> bool:
        """False when the spool is above ``max_bytes`` or the filesystem has
        less than ``min_free_fraction`` free. Never deletes anything."""
        if self.stats().total_bytes > self.max_bytes:
            return False
        u = usage or shutil.disk_usage(self.root)
        return u.free >= self.min_free_fraction * u.total
