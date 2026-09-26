"""Server-side segmentation of raw segments into speech chunks (ADR 0005, plan §2.3).

Raw segments arrive as independent 30.0 s Ogg Opus files that are sample-contiguous within
one `(device_id, run_id, epoch)`. Per epoch, ordered by `n_start`, the contiguous run is
decoded back into one PCM stream, fed in 512-sample windows to the VAD and the same
`Chunker` the edge uses, and every closed chunk becomes a `chunks` row with
`kind='derived'`, no file, and `sha256` = SHA-256 of its int16 LE PCM slice.

Rules that make repeated passes safe:

* A pass processes through the last segment whose successor has arrived. The last segment is
  included when it ends the epoch itself (`cut_reason` discontinuity/shutdown) or when the
  epoch has been idle for `raw_idle_s`; then the chunker is cut with that reason.
* `segmented_to_n` is the high-water mark: everything before it is final. A pass decodes from
  about 35 s before the mark (VAD warm-up plus the longest possible open chunk), on the
  window grid anchored at the chain start so every pass sees the same windows, and commits
  chunks that end after the mark, clipped to start at the mark. Chunks never overlap, and a
  re-derived slice hashes to the same sha and is ignored on insert.
* The mark advances to the start of the still-open chunk (or to the decode end when nothing
  is open), so progress is at least `decode_end - 30 s` per pass and silence never causes a
  re-decode of the whole epoch.
* A segment that breaks contiguity starts its own chain with a fresh chunker at its origin.
  A late segment that fills a gap behind the mark is segmented in isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import vad as vadmod
from .audio import SAMPLE_RATE, decode_opus
from .chunker import Chunk as Cut, Chunker
from .times import ms_to_iso, now_ms

log = logging.getLogger("roomlog.segmenter")

WINDOW = vadmod.WINDOW
WARMUP_SAMPLES = 35 * SAMPLE_RATE  # > max chunk (30 s) + padding + min silence
MAX_SPAN_SAMPLES = 10 * 60 * SAMPLE_RATE  # decode at most this much per pass (38 MB float32)


class EnergyVad:
    """RMS gate with the `Vad` interface: tests, and running without the Silero model."""

    def __init__(self, threshold_dbfs: float = -40.0) -> None:
        self.threshold_dbfs = threshold_dbfs

    def __call__(self, window: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(window * window)))
        dbfs = 20 * np.log10(rms) if rms > 0 else -120.0
        return 0.95 if dbfs > self.threshold_dbfs else 0.02

    def reset(self) -> None:
        pass


def vad_factory_from_config(cfg: Any) -> tuple[Callable[[], vadmod.Vad], dict[str, Any]]:
    """The VAD constructor for the worker and CLI, plus the `vad` object for derived sidecars."""
    if cfg.vad_backend == "energy":
        return EnergyVad, {"model": "energy", "version": "rms-gate", "threshold": cfg.vad_threshold}
    model_path = Path(cfg.vad_model_path)
    sha_file = model_path.with_name(model_path.name + ".sha256")
    expected = sha_file.read_text().split()[0] if sha_file.exists() else None

    def make() -> vadmod.Vad:
        if not model_path.exists():
            raise FileNotFoundError(f"Silero model missing at {model_path}; run `roomlog fetch-model`")
        return vadmod.SileroVad(model_path, expected_sha256=expected)

    return make, vadmod.info(cfg.vad_threshold)


def segmenter_from_config(cfg: Any, conn: sqlite3.Connection, now: Callable[[], int] = now_ms) -> "Segmenter":
    factory, info = vad_factory_from_config(cfg)
    return Segmenter(conn, cfg.archive_dir, factory, vad_info=info, now=now, raw_idle_s=cfg.raw_idle_s,
                     threshold=cfg.vad_threshold, neg_threshold=cfg.vad_neg_threshold)


@dataclass
class RawSeg:
    id: int
    sha256: str
    device_id: str
    run_id: str
    epoch: int
    n_start: int
    n_samples: int
    start_utc_ms: int
    discontinuity: bool
    clock_synced: bool
    cut_reason: str
    path: str
    status: str
    received_utc_ms: int
    edge_version: str | None

    @property
    def n_end(self) -> int:
        return self.n_start + self.n_samples

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "RawSeg":
        edge_version = None
        try:
            edge_version = json.loads(r["meta_json"]).get("edge_version")
        except ValueError:
            pass
        return cls(id=r["id"], sha256=r["sha256"], device_id=r["device_id"], run_id=r["run_id"],
                   epoch=r["epoch"], n_start=r["n_start"], n_samples=r["n_samples"],
                   start_utc_ms=r["start_utc_ms"], discontinuity=bool(r["discontinuity"]),
                   clock_synced=bool(r["clock_synced"]), cut_reason=r["cut_reason"], path=r["path"],
                   status=r["status"], received_utc_ms=r["received_utc_ms"], edge_version=edge_version)


@dataclass
class SegmentResult:
    epochs: int = 0
    chunks: int = 0
    raw_segmented: int = 0
    errors: list[str] = field(default_factory=list)


def split_chains(segs: list[RawSeg]) -> list[list[RawSeg]]:
    """Maximal sample-contiguous runs, in order. `segs` must be sorted by `n_start`."""
    chains: list[list[RawSeg]] = []
    for s in segs:
        if chains and chains[-1][-1].n_end == s.n_start:
            chains[-1].append(s)
        else:
            chains.append([s])
    return chains


def pcm_int16(audio: np.ndarray) -> np.ndarray:
    """float32 in [-1, 1] → int16 LE, the bytes a derived chunk's sha256 is taken over."""
    return np.clip(np.rint(audio.astype(np.float64) * 32768.0), -32768, 32767).astype("<i2")


def pcm_sha256(audio: np.ndarray) -> str:
    return hashlib.sha256(pcm_int16(audio).tobytes()).hexdigest()


def samples_to_ms(n: int) -> int:
    return (n * 1000 + SAMPLE_RATE // 2) // SAMPLE_RATE


def covering_segments(conn: sqlite3.Connection, device_id: str, run_id: str, epoch: int,
                      n_start: int, n_end: int) -> list[RawSeg]:
    rows = conn.execute(
        """SELECT * FROM raw_segments
           WHERE device_id = ? AND run_id = ? AND epoch = ? AND n_start < ? AND n_start + n_samples > ?
           ORDER BY n_start""",
        (device_id, run_id, epoch, n_end, n_start),
    ).fetchall()
    return [RawSeg.from_row(r) for r in rows]


class RawAudio:
    """Decoded raw segments, cached by sha for the life of one pass or worker batch."""

    def __init__(self, archive_dir: Path, decode: Callable[[str], np.ndarray] = decode_opus) -> None:
        self.archive_dir = archive_dir
        self.decode = decode
        self._cache: dict[str, np.ndarray] = {}

    def segment(self, seg: RawSeg) -> np.ndarray:
        a = self._cache.get(seg.sha256)
        if a is None:
            a = self.decode(str(self.archive_dir / seg.path))
            if len(a) != seg.n_samples:
                # The seams are only sample-exact when the file decodes to exactly n_samples.
                log.warning("raw %s decoded to %d samples, sidecar says %d; fitting",
                            seg.path, len(a), seg.n_samples)
                fitted = np.zeros(seg.n_samples, dtype=np.float32)
                k = min(len(a), seg.n_samples)
                fitted[:k] = a[:k]
                a = fitted
            self._cache[seg.sha256] = a.astype(np.float32, copy=False)
        return self._cache[seg.sha256]

    def span(self, segs: list[RawSeg], lo: int, hi: int) -> np.ndarray:
        """Samples `[lo, hi)` of the contiguous chain `segs`; raises when not fully covered."""
        parts: list[np.ndarray] = []
        pos = lo
        for s in segs:
            if s.n_end <= lo or s.n_start >= hi:
                continue
            if s.n_start > pos:
                raise ValueError(f"raw audio gap at n={pos}..{s.n_start}")
            a = self.segment(s)
            parts.append(a[max(lo, s.n_start) - s.n_start: min(hi, s.n_end) - s.n_start])
            pos = min(hi, s.n_end)
        if pos < hi:
            raise ValueError(f"raw audio ends at n={pos}, wanted {hi}")
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def keep_only(self, segs: list[RawSeg]) -> None:
        keep = {s.sha256 for s in segs}
        self._cache = {k: v for k, v in self._cache.items() if k in keep}


class Segmenter:
    def __init__(self, conn: sqlite3.Connection, archive_dir: Path,
                 vad_factory: Callable[[], vadmod.Vad],
                 vad_info: dict[str, Any] | None = None,
                 decode: Callable[[str], np.ndarray] = decode_opus,
                 now: Callable[[], int] = now_ms,
                 raw_idle_s: float = 120.0,
                 threshold: float = 0.5, neg_threshold: float = 0.35,
                 chunker_kwargs: dict[str, Any] | None = None) -> None:
        self.conn = conn
        self.archive_dir = archive_dir
        self.vad_factory = vad_factory
        self.vad_info = vad_info if vad_info is not None else vadmod.info(threshold)
        self.now = now
        self.raw_idle_s = raw_idle_s
        self.threshold = threshold
        self.neg_threshold = neg_threshold
        self.chunker_kwargs = chunker_kwargs or {}
        self.audio = RawAudio(archive_dir, decode)
        self._vad: vadmod.Vad | None = None

    # -- public

    def run_once(self) -> SegmentResult:
        res = SegmentResult()
        keys = self.conn.execute(
            """SELECT DISTINCT device_id, run_id, epoch FROM raw_segments WHERE status = 'pending'
               ORDER BY device_id, run_id, epoch"""
        ).fetchall()
        for k in keys:
            try:
                self._process_epoch(k["device_id"], k["run_id"], k["epoch"], res)
            except Exception as e:
                log.exception("segmenting %s/%s/%d failed", k["device_id"], k["run_id"], k["epoch"])
                res.errors.append(f"{k['device_id']}/{k['run_id']}/{k['epoch']}: {e.__class__.__name__}: {e}")
                if isinstance(e, FileNotFoundError):
                    break  # the model is missing: every epoch would fail the same way
        self.audio.keep_only([])
        return res

    # -- one epoch

    def _vad_instance(self) -> vadmod.Vad:
        if self._vad is None:
            self._vad = self.vad_factory()
        return self._vad

    def _load(self, device_id: str, run_id: str, epoch: int) -> list[RawSeg]:
        rows = self.conn.execute(
            "SELECT * FROM raw_segments WHERE device_id = ? AND run_id = ? AND epoch = ? ORDER BY n_start, id",
            (device_id, run_id, epoch),
        ).fetchall()
        segs: list[RawSeg] = []
        for r in rows:
            s = RawSeg.from_row(r)
            if segs and s.n_start < segs[-1].n_end:
                log.warning("raw %s overlaps %s; ignoring the later one", s.path, segs[-1].path)
                if s.status == "pending":
                    self.conn.execute(
                        "UPDATE raw_segments SET status = 'segmented', segmented_utc_ms = ? WHERE id = ?",
                        (self.now(), s.id))
                continue
            segs.append(s)
        return segs

    def _progress(self, device_id: str, run_id: str, epoch: int) -> tuple[int | None, int | None]:
        r = self.conn.execute(
            "SELECT segmented_to_n, scanned_to_n FROM raw_progress WHERE device_id = ? AND run_id = ? AND epoch = ?",
            (device_id, run_id, epoch),
        ).fetchone()
        return (r["segmented_to_n"], r["scanned_to_n"]) if r else (None, None)

    def _process_epoch(self, device_id: str, run_id: str, epoch: int, res: SegmentResult) -> None:
        segs = self._load(device_id, run_id, epoch)
        if not segs:
            return
        mark, scanned = self._progress(device_id, run_id, epoch)
        idle = (self.now() - max(s.received_utc_ms for s in segs)) >= self.raw_idle_s * 1000
        worked = False
        for chain in split_chains(segs):
            pending = [s for s in chain if s.status == "pending"]
            if not pending:
                continue
            chain_start, chain_end = chain[0].n_start, chain[-1].n_end
            if mark is not None and mark >= chain_end:
                # Late arrivals behind the mark: their neighbours are final, so each pending
                # run is segmented on its own.
                for run in split_chains(pending):
                    self._pass(chain, run[0].n_start, run[-1].n_end, run[0].n_start, "shutdown",
                               warmup=False, res=res)
                    self._mark_segmented(run)
                worked = True
                continue
            pos = chain_start if (mark is None or mark < chain_start) else mark
            fill = [s for s in pending if s.n_end <= pos]
            for run in split_chains(fill):
                self._pass(chain, run[0].n_start, run[-1].n_end, run[0].n_start, "shutdown",
                           warmup=False, res=res)
                self._mark_segmented(run)
                worked = True
            last = chain[-1]
            if last.cut_reason != "cap":
                final, hi = last.cut_reason, chain_end  # the epoch ended; no successor will come
            elif idle:
                final, hi = "shutdown", chain_end
            else:
                final, hi = None, last.n_start  # wait for the successor before closing anything
            if hi <= pos:
                continue
            if final is None and scanned is not None and pos == mark and hi <= scanned:
                continue  # nothing new since the last pass
            while True:
                sub_hi = min(hi, pos + MAX_SPAN_SAMPLES)
                sub_final = final if sub_hi == hi else None
                new_mark = self._pass(chain, pos, sub_hi, pos, sub_final, warmup=True, res=res)
                new_mark = max(new_mark, pos)
                self._save_progress(device_id, run_id, epoch, new_mark, sub_hi)
                self._mark_segmented([s for s in chain if s.status == "pending" and s.n_end <= new_mark])
                self.audio.keep_only([s for s in chain if s.n_end > new_mark - WARMUP_SAMPLES])
                worked = True
                mark, scanned = new_mark, sub_hi
                if sub_hi == hi or new_mark <= pos:
                    break
                pos = new_mark
        if worked:
            res.epochs += 1

    # -- one decode-and-chunk pass over `[lo, hi)` of a chain

    def _pass(self, chain: list[RawSeg], lo: int, hi: int, commit_from: int, final: str | None,
              warmup: bool, res: SegmentResult) -> int:
        """Returns the new high-water mark."""
        chain_start = chain[0].n_start
        decode_lo = lo
        if warmup:
            # On the window grid anchored at the chain start, so repeated passes see identical
            # windows and the chunker makes identical decisions around the mark.
            aligned = chain_start + ((lo - WARMUP_SAMPLES - chain_start) // WINDOW) * WINDOW
            decode_lo = max(chain_start, aligned)
        audio = self.audio.span(chain, decode_lo, hi)
        vad = self._vad_instance()
        vad.reset()
        chunker = Chunker.from_ms(threshold=self.threshold, neg_threshold=self.neg_threshold,
                                  **self.chunker_kwargs)
        chunker.reset(decode_lo)
        cuts: list[Cut] = []
        for i in range(len(audio) // WINDOW):
            p = vad(audio[i * WINDOW:(i + 1) * WINDOW])
            cuts.extend(chunker.feed(i, p))
        if final is not None:
            cuts.extend(chunker.cut(final))

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for c in cuts:
                if c.n_end <= commit_from:
                    continue
                n_start = max(c.n_start, commit_from)
                if n_start != c.n_start and c.n_end - n_start < chunker.min_speech:
                    continue  # the sliver of an already-committed chunk
                slice_ = audio[n_start - decode_lo: c.n_end - decode_lo]
                if self._insert_derived(chain, n_start, c.n_end, c.cut_reason, slice_):
                    res.chunks += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        if final is not None:
            return hi
        if chunker.open:
            return max(commit_from, chunker._chunk_start)  # noqa: SLF001 (padded start of the open chunk)
        return hi

    def _insert_derived(self, chain: list[RawSeg], n_start: int, n_end: int, cut_reason: str,
                        audio: np.ndarray) -> bool:
        seg = next(s for s in chain if s.n_start <= n_start < s.n_end)
        covering = [s for s in chain if s.n_end > n_start and s.n_start < n_end]
        sha = pcm_sha256(audio)
        n_samples = n_end - n_start
        start_ms = seg.start_utc_ms + samples_to_ms(n_start - seg.n_start)
        duration_ms = samples_to_ms(n_samples)
        discontinuity = False
        if seg.discontinuity:
            # Only the first chunk after the epoch boundary the raw segment carries.
            earlier = self.conn.execute(
                """SELECT 1 FROM chunks WHERE device_id = ? AND run_id = ? AND epoch = ?
                   AND kind = 'derived' AND n_start < ? LIMIT 1""",
                (seg.device_id, seg.run_id, seg.epoch, n_start),
            ).fetchone()
            discontinuity = earlier is None
        meta = {
            "schema_version": 1,
            "kind": "derived",
            "device_id": seg.device_id,
            "sha256": sha,
            "start_utc": ms_to_iso(start_ms),
            "duration_s": round(n_samples / SAMPLE_RATE, 3),
            "sample_rate": SAMPLE_RATE,
            "run_id": seg.run_id,
            "epoch": seg.epoch,
            "n_start": n_start,
            "n_samples": n_samples,
            "discontinuity": discontinuity,
            "clock_step": False,
            "clock_synced": seg.clock_synced,
            "cut_reason": cut_reason,
            "vad": dict(self.vad_info),
            "edge_version": seg.edge_version,
            "derived_from": [s.sha256 for s in covering],
            "session_hint": None,
            "multi_speaker": None,
        }
        cur = self.conn.execute(
            """INSERT INTO chunks (sha256, kind, device_id, start_utc_ms, end_utc_ms, duration_ms, path,
                                   n_start, n_samples, meta_json, run_id, epoch, discontinuity,
                                   clock_synced, received_utc_ms, status)
               VALUES (?, 'derived', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(sha256) DO NOTHING""",
            (sha, seg.device_id, start_ms, start_ms + duration_ms, duration_ms, n_start, n_samples,
             json.dumps(meta, ensure_ascii=True, separators=(",", ":")), seg.run_id, seg.epoch,
             1 if discontinuity else 0, 1 if seg.clock_synced else 0, self.now()),
        )
        return cur.rowcount == 1

    def _save_progress(self, device_id: str, run_id: str, epoch: int, mark: int, scanned: int) -> None:
        self.conn.execute(
            """INSERT INTO raw_progress (device_id, run_id, epoch, segmented_to_n, scanned_to_n, updated_utc_ms)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, run_id, epoch) DO UPDATE SET
                   segmented_to_n = excluded.segmented_to_n, scanned_to_n = excluded.scanned_to_n,
                   updated_utc_ms = excluded.updated_utc_ms""",
            (device_id, run_id, epoch, mark, scanned, self.now()),
        )

    def _mark_segmented(self, segs: list[RawSeg]) -> None:
        if not segs:
            return
        ts = self.now()
        self.conn.executemany(
            "UPDATE raw_segments SET status = 'segmented', segmented_utc_ms = ? WHERE id = ? AND status = 'pending'",
            [(ts, s.id) for s in segs],
        )
        for s in segs:
            s.status = "segmented"


# ---------------------------------------------------------------- resegment and purge

RAW_RETENTION_DAYS = 30  # fixed in v1, config in v2 (plan decision 6)


def reset_segmentation(conn: sqlite3.Connection, device_id: str, from_ms: int | None = None,
                       to_ms: int | None = None) -> dict[str, int]:
    """Undo segmentation for a device's raw segments starting in `[from_ms, to_ms)`.

    Per affected epoch the progress mark moves back to the first reset segment; derived
    chunks from that point on (and their segments, by cascade) are deleted and the raw
    segments go back to `pending`, so the next `segment` pass re-derives them.
    """
    where = ["device_id = ?"]
    params: list[Any] = [device_id]
    if from_ms is not None:
        where.append("start_utc_ms >= ?")
        params.append(from_ms)
    if to_ms is not None:
        where.append("start_utc_ms < ?")
        params.append(to_ms)
    rows = conn.execute(
        f"""SELECT run_id, epoch, min(n_start) AS n_from, count(*) AS n
            FROM raw_segments WHERE {' AND '.join(where)} GROUP BY run_id, epoch""",
        params,
    ).fetchall()
    out = {"epochs": 0, "raw_reset": 0, "chunks_deleted": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            out["epochs"] += 1
            out["raw_reset"] += r["n"]
            # A derived chunk straddling the reset point is deleted too, and the mark moves
            # back to its start so the next pass re-derives it whole instead of clipped.
            first = conn.execute(
                """SELECT min(n_start) FROM chunks WHERE device_id = ? AND run_id = ? AND epoch = ?
                   AND kind = 'derived' AND n_start + n_samples > ?""",
                (device_id, r["run_id"], r["epoch"], r["n_from"]),
            ).fetchone()[0]
            n_from = r["n_from"] if first is None else min(r["n_from"], first)
            cur = conn.execute(
                """DELETE FROM chunks WHERE device_id = ? AND run_id = ? AND epoch = ?
                   AND kind = 'derived' AND n_start + n_samples > ?""",
                (device_id, r["run_id"], r["epoch"], n_from),
            )
            out["chunks_deleted"] += cur.rowcount
            conn.execute(
                """UPDATE raw_segments SET status = 'pending', segmented_utc_ms = NULL
                   WHERE device_id = ? AND run_id = ? AND epoch = ? AND n_start + n_samples > ?""",
                (device_id, r["run_id"], r["epoch"], n_from),
            )
            epoch_start = conn.execute(
                "SELECT min(n_start) FROM raw_segments WHERE device_id = ? AND run_id = ? AND epoch = ?",
                (device_id, r["run_id"], r["epoch"])).fetchone()[0]
            if n_from <= epoch_start:
                conn.execute("DELETE FROM raw_progress WHERE device_id = ? AND run_id = ? AND epoch = ?",
                             (device_id, r["run_id"], r["epoch"]))
            else:
                conn.execute(
                    """UPDATE raw_progress SET segmented_to_n = min(segmented_to_n, ?),
                           scanned_to_n = min(scanned_to_n, ?)
                       WHERE device_id = ? AND run_id = ? AND epoch = ?""",
                    (n_from, n_from, device_id, r["run_id"], r["epoch"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


def purge_raw(conn: sqlite3.Connection, archive_dir: Path, now: int | None = None,
              retention_days: int = RAW_RETENTION_DAYS) -> dict[str, int]:
    """Delete segmented raw segments older than the retention that overlap no chunk.

    "Older" means both the audio (`start_utc_ms`) and the row (`received_utc_ms`) are past
    the retention; a raw segment that any derived chunk still references is never touched.
    """
    now = now_ms() if now is None else now
    cutoff = now - retention_days * 86_400_000
    rows = conn.execute(
        """SELECT r.id, r.path FROM raw_segments r
           WHERE r.status = 'segmented' AND r.start_utc_ms < ? AND r.received_utc_ms < ?
             AND NOT EXISTS (
                 SELECT 1 FROM chunks c
                 WHERE c.device_id = r.device_id AND c.run_id = r.run_id AND c.epoch = r.epoch
                   AND c.kind = 'derived'
                   AND c.n_start < r.n_start + r.n_samples AND c.n_start + c.n_samples > r.n_start)
           ORDER BY r.start_utc_ms""",
        (cutoff, cutoff),
    ).fetchall()
    out = {"deleted": 0, "bytes": 0}
    for r in rows:
        opus = archive_dir / r["path"]
        for p in (opus, opus.with_suffix(".json")):
            try:
                out["bytes"] += p.stat().st_size
                p.unlink()
            except FileNotFoundError:
                pass
        conn.execute("DELETE FROM raw_segments WHERE id = ?", (r["id"],))
        out["deleted"] += 1
        parent = opus.parent
        while parent != archive_dir and parent.name and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return out
