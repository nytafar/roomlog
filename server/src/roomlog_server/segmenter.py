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
* `segmented_to_n` is the high-water mark: every derived chunk ends at or before it, and
  nothing before it is derived again. A pass decodes from about 35 s before the mark (VAD
  warm-up plus the longest possible open chunk) on the window grid anchored at the chain
  start, so every pass sees the same windows, and commits the chunks that end at or before
  the new mark, clipped to start at the old one.
* The new mark is a point the replay reaches in the same chunker state (`_next_mark`): the
  padded start of an open chunk, the start of a cap-cut burst, or `pad` before the decode
  end when nothing is open, so speech that starts just after a seam keeps its pre-roll.
  Progress is therefore at least `decode_end - 30 s` per pass outside very long bursts.
* A segment that breaks contiguity starts its own chain with a fresh chunker at its origin.
  A gap inside an epoch counts as final once it has been visible for `raw_idle_s`; until
  then the chain before it waits for its successor and the chains after it wait behind it,
  because the mark is one per epoch and must never pass an open chain. A late segment that
  fills a gap behind the mark is segmented in place, between the surviving chunks.
* A raw file that does not decode to its `n_samples` (beyond a few samples of codec
  priming) is marked `failed` and left out of its chain, which then has a gap there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import sdnotify
from . import vad as vadmod
from .audio import SAMPLE_RATE, decode_opus
from .chunker import Chunk as Cut, Chunker
from .times import ms_to_iso, now_ms

log = logging.getLogger("roomlog.segmenter")

WINDOW = vadmod.WINDOW
WARMUP_SAMPLES = 35 * SAMPLE_RATE  # > max chunk (30 s) + padding + min silence
MAX_SPAN_SAMPLES = 10 * 60 * SAMPLE_RATE  # decode at most this much per pass (38 MB float32)
MAX_BURST_SAMPLES = 3 * 60 * SAMPLE_RATE  # a cap-cut burst longer than this is committed mid-burst


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
                     threshold=cfg.vad_threshold, neg_threshold=cfg.vad_neg_threshold,
                     heartbeat=lambda: sdnotify.notify("WATCHDOG=1"))


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
    raw_failed: int = 0  # raw segments that do not decode to their n_samples; now status 'failed'
    more: bool = False  # the call stopped at its budget or a failed segment; call again soon
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


def dedupe_overlaps(segs: list[RawSeg], on_drop: Callable[[RawSeg, RawSeg], None] | None = None) -> list[RawSeg]:
    """Drop every row that overlaps an earlier kept row. `segs` sorted by `(n_start, id)`.

    Two uploads of one span (a re-encode after a failed upload has a new sha) must not be
    played twice: the segmenter and the worker's `audio_for` both keep the first row.
    """
    out: list[RawSeg] = []
    for s in segs:
        if out and s.n_start < out[-1].n_end:
            if on_drop is not None:
                on_drop(s, out[-1])
            continue
        out.append(s)
    return out


def covering_segments(conn: sqlite3.Connection, device_id: str, run_id: str, epoch: int,
                      n_start: int, n_end: int) -> list[RawSeg]:
    """The rows the worker and `verify` play a derived chunk from: the same rows the
    segmenter chained, so `failed` uploads are out even when a good re-upload came later."""
    rows = conn.execute(
        """SELECT * FROM raw_segments
           WHERE device_id = ? AND run_id = ? AND epoch = ? AND n_start < ? AND n_start + n_samples > ?
             AND status != 'failed'
           ORDER BY n_start, id""",
        (device_id, run_id, epoch, n_end, n_start),
    ).fetchall()
    return dedupe_overlaps([RawSeg.from_row(r) for r in rows])


def raw_covers(segs: list[RawSeg], lo: int, hi: int) -> bool:
    """True when `segs` (sorted) cover `[lo, hi)` without a gap."""
    pos = lo
    for s in segs:
        if s.n_end <= pos:
            continue
        if s.n_start > pos:
            return False
        pos = s.n_end
        if pos >= hi:
            return True
    return pos >= hi


class RawDecodeError(Exception):
    """A raw segment did not decode to the sample count its sidecar promised."""

    def __init__(self, seg: RawSeg, decoded: int) -> None:
        super().__init__(f"raw {seg.path} decoded to {decoded} samples, sidecar says {seg.n_samples}")
        self.seg = seg
        self.decoded = decoded


DECODE_TOLERANCE = 64  # samples; PyAV is exact at 16 kHz beyond the first ~50 samples of a file


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
                # The seams are only sample-exact when the file decodes to exactly n_samples
                # (CONTRACT.md: duration_s == n_samples / 16000 within 1 ms). A few samples of
                # codec priming are fitted; anything more is a broken file, not a rounding.
                if abs(len(a) - seg.n_samples) > DECODE_TOLERANCE:
                    raise RawDecodeError(seg, len(a))
                log.debug("raw %s decoded to %d samples, sidecar says %d; fitting",
                          seg.path, len(a), seg.n_samples)
                fitted = np.zeros(seg.n_samples, dtype=np.float32)
                k = min(len(a), seg.n_samples)
                fitted[:k] = a[:k]
                a = fitted
            self._cache[seg.sha256] = a.astype(np.float32, copy=False)
        return self._cache[seg.sha256]

    def span(self, segs: list[RawSeg], lo: int, hi: int) -> np.ndarray:
        """Samples `[lo, hi)` of the contiguous chain `segs`; raises when not fully covered.

        A row that starts before the samples already taken contributes only its remainder,
        so a duplicate upload of one span is played once.
        """
        parts: list[np.ndarray] = []
        pos = lo
        for s in segs:
            if s.n_end <= pos or s.n_start >= hi:
                continue
            if s.n_start > pos:
                raise ValueError(f"raw audio gap at n={pos}..{s.n_start}")
            a = self.segment(s)
            parts.append(a[pos - s.n_start: min(hi, s.n_end) - s.n_start])
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
                 chunker_kwargs: dict[str, Any] | None = None,
                 heartbeat: Callable[[], None] | None = None) -> None:
        self.conn = conn
        self.archive_dir = archive_dir
        self.vad_factory = vad_factory
        self.vad_info = vad_info if vad_info is not None else vadmod.info(threshold)
        self.now = now
        self.raw_idle_s = raw_idle_s
        self.threshold = threshold
        self.neg_threshold = neg_threshold
        self.chunker_kwargs = chunker_kwargs or {}
        self.heartbeat = heartbeat or (lambda: None)
        self.audio = RawAudio(archive_dir, decode)
        self._vad: vadmod.Vad | None = None

    # -- public

    def run_once(self, budget_s: float | None = None) -> SegmentResult:
        """Segment pending epochs, oldest device/run first.

        `budget_s` caps the wall time of one call: the pass that crosses it is finished, the
        rest of the backlog waits for the next call (`res.more` says so). `heartbeat` runs
        before every decode pass, so a systemd watchdog sees progress through a long backlog.
        """
        res = SegmentResult()
        started = time.monotonic()
        keys = self.conn.execute(
            """SELECT DISTINCT device_id, run_id, epoch FROM raw_segments WHERE status = 'pending'
               ORDER BY device_id, run_id, epoch"""
        ).fetchall()
        for i, k in enumerate(keys):
            if i and budget_s is not None and time.monotonic() - started >= budget_s:
                res.more = True
                break
            try:
                self._process_epoch(k["device_id"], k["run_id"], k["epoch"], res)
            except RawDecodeError as e:
                log.error("segmenting %s/%s/%d: %s", k["device_id"], k["run_id"], k["epoch"], e)
                res.errors.append(f"{k['device_id']}/{k['run_id']}/{k['epoch']}: {e}")
                if e.seg.status == "pending":
                    # Out of the chain for good: its neighbours become a gap and go on without it.
                    self.conn.execute("UPDATE raw_segments SET status = 'failed' WHERE id = ? AND status = 'pending'",
                                      (e.seg.id,))
                    res.raw_failed += 1
                    res.more = True
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
            """SELECT * FROM raw_segments WHERE device_id = ? AND run_id = ? AND epoch = ? AND status != 'failed'
               ORDER BY n_start, id""",
            (device_id, run_id, epoch),
        ).fetchall()

        def dropped(s: RawSeg, kept: RawSeg) -> None:
            log.warning("raw %s overlaps %s; ignoring the later one", s.path, kept.path)
            if s.status == "pending":
                self.conn.execute(
                    "UPDATE raw_segments SET status = 'segmented', segmented_utc_ms = ? WHERE id = ?",
                    (self.now(), s.id))

        return dedupe_overlaps([RawSeg.from_row(r) for r in rows], on_drop=dropped)

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
        now = self.now()
        idle_ms = int(self.raw_idle_s * 1000)
        idle = (now - max(s.received_utc_ms for s in segs)) >= idle_ms
        worked = False
        chains = split_chains(segs)
        for ci, chain in enumerate(chains):
            pending = [s for s in chain if s.status == "pending"]
            if not pending:
                continue
            chain_start, chain_end = chain[0].n_start, chain[-1].n_end
            if mark is not None and mark >= chain_end:
                # Late arrivals behind the mark: their neighbours are final, so each pending
                # run is segmented on its own, around whatever chunks already exist.
                for run in split_chains(pending):
                    self._fill(chain, run, res)
                worked = True
                continue
            pos = chain_start if (mark is None or mark < chain_start) else mark
            fill = [s for s in pending if s.n_end <= pos]
            for run in split_chains(fill):
                self._fill(chain, run, res)
                worked = True
            last = chain[-1]
            later = chains[ci + 1:]
            if later:
                # A gap after this chain. It may still fill (an upload retried out of order),
                # so it is treated as final only once it has been visible for raw_idle_s.
                seen_since = max(last.received_utc_ms, min(s.received_utc_ms for c in later for s in c))
                gap_final = now - seen_since >= idle_ms
            else:
                gap_final = False
            if last.cut_reason != "cap":
                final, hi = last.cut_reason, chain_end  # the epoch ended; no successor will come
            elif idle or gap_final:
                final, hi = "shutdown", chain_end
            else:
                final, hi = None, last.n_start  # wait for the successor before closing anything
            stale = final is None and scanned is not None and pos == mark and hi <= scanned
            if hi > pos and not stale:
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
            if final is None:
                # This chain waits for its successor. The mark is one per epoch, so the chains
                # after the gap wait too: were they segmented now, the mark would pass this
                # chain's open tail and the tail would later be re-derived against them.
                break
        if worked:
            res.epochs += 1

    def _fill(self, chain: list[RawSeg], run: list[RawSeg], res: SegmentResult) -> None:
        """Segment a pending run behind the mark, only where no derived chunk exists yet.

        The run is a former gap (or a `resegment` range) inside a chain whose other parts
        are final, so every free stretch is cut with `shutdown` at its end. The chunker warms
        up over the neighbours on the chain's window grid, so decisions at the run's start
        match what a one-shot pass would have made.
        """
        lo, hi = run[0].n_start, run[-1].n_end
        first = run[0]
        survivors = self.conn.execute(
            """SELECT n_start, n_start + n_samples AS n_end FROM chunks
               WHERE device_id = ? AND run_id = ? AND epoch = ? AND kind = 'derived'
                 AND n_start < ? AND n_start + n_samples > ? ORDER BY n_start""",
            (first.device_id, first.run_id, first.epoch, hi, lo),
        ).fetchall()
        pos = lo
        for c in survivors:
            if c["n_start"] > pos:
                self._pass(chain, pos, c["n_start"], pos, "shutdown", warmup=True, res=res)
            pos = max(pos, c["n_end"])
        if pos < hi:
            self._pass(chain, pos, hi, pos, "shutdown", warmup=True, res=res)
        self._mark_segmented(run)

    # -- one decode-and-chunk pass over `[lo, hi)` of a chain

    def _pass(self, chain: list[RawSeg], lo: int, hi: int, commit_from: int, final: str | None,
              warmup: bool, res: SegmentResult) -> int:
        """Chunk `[lo, hi)`, commit the chunks inside `[commit_from, hi)`, return the new mark."""
        self.heartbeat()
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
        n_full = len(audio) // WINDOW
        for i in range(n_full):
            p = vad(audio[i * WINDOW:(i + 1) * WINDOW])
            cuts.extend(chunker.feed(i, p))
        if final is not None:
            if len(audio) % WINDOW:
                # The last partial window before the end of the audio, zero-padded: the cut
                # below closes at `hi`, so an open chunk keeps its last few hundred samples.
                tail = np.zeros(WINDOW, dtype=np.float32)
                tail[:len(audio) - n_full * WINDOW] = audio[n_full * WINDOW:]
                cuts.extend(chunker.feed(n_full, vad(tail)))
            cuts.extend(chunker.cut(final, n_end=hi))

        mark = self._next_mark(chunker, cuts, hi, commit_from, final)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for n_start, n_end, reason in self._committable(cuts, commit_from, mark, chunker.min_speech):
                slice_ = audio[n_start - decode_lo: n_end - decode_lo]
                if self._insert_derived(chain, n_start, n_end, reason, slice_):
                    res.chunks += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return mark

    @staticmethod
    def _next_mark(chunker: Chunker, cuts: list[Cut], hi: int, commit_from: int, final: str | None) -> int:
        """Where the next pass takes over. Everything before it is committed by this pass.

        The point is chosen so a replay from 35 s earlier reaches it in the same chunker state
        as this pass did, which makes the chunk set independent of how the audio arrived:

        * `final`: the chain ends here; `hi`.
        * nothing open: the last `pad` before `hi` stays uncommitted so speech starting just
          after `hi` keeps its pre-roll; never behind the last emitted chunk.
        * an open chunk that started after a silence: its padded start.
        * an open continuation after cap cuts: the start of the burst (the first cap chunk),
          because cap positions depend on where the burst began. A burst longer than
          `MAX_BURST_SAMPLES` past the mark is committed anyway so progress never stalls;
          the replay then re-cuts its tail at other pauses, which `_committable` keeps whole.
        """
        if final is not None:
            return hi
        if not chunker.open:
            return max(commit_from, min(hi, max(chunker._prev_end, chunker._last_end - chunker.pad)))  # noqa: SLF001
        open_start = chunker._chunk_start  # noqa: SLF001
        if not chunker._continued:  # noqa: SLF001
            return max(commit_from, open_start)
        burst_start = open_start
        for c in reversed(cuts):
            if c.n_end != burst_start or c.cut_reason != "cap":
                break
            burst_start = c.n_start
        if open_start - max(burst_start, commit_from) > MAX_BURST_SAMPLES:
            return max(commit_from, open_start)
        return max(commit_from, burst_start)

    @staticmethod
    def _committable(cuts: list[Cut], commit_from: int, commit_to: int,
                     min_speech: int) -> list[tuple[int, int, str]]:
        """Cuts clipped to `[commit_from, commit_to)`.

        A cut that straddles `commit_from` (a replay that cut a long burst at other pauses than
        the pass that committed the audio before the mark) keeps its remainder: merged into
        the contiguous next cut when it is shorter than `min_speech`, else as a chunk of its own.
        """
        out: list[tuple[int, int, str]] = []
        for c in cuts:
            n_start, n_end = max(c.n_start, commit_from), min(c.n_end, commit_to)
            if n_end <= n_start:
                continue
            if out and out[-1][1] == n_start and out[-1][1] - out[-1][0] < min_speech and out[-1][0] == commit_from:
                n_start = out.pop()[0]
            out.append((n_start, n_end, c.cut_reason))
        return out

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

    Per affected epoch, the derived chunks that intersect the selected samples are deleted
    (their transcript segments go by cascade) and the raw segments under them go back to
    `pending`. A chunk straddling either end is deleted whole and its whole extent is redone,
    so the reset never leaves half a chunk. When the extent reaches the epoch's progress
    mark, the mark moves back to its start and the whole tail from there (chunks after the
    extent included) is re-derived in order; otherwise chunks outside the extent stay and
    the next pass re-derives the extent in place, between the surviving neighbours, like a
    gap that filled late.
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
        f"""SELECT run_id, epoch, min(n_start) AS n_from, max(n_start + n_samples) AS n_to
            FROM raw_segments WHERE {' AND '.join(where)} GROUP BY run_id, epoch""",
        params,
    ).fetchall()
    out = {"epochs": 0, "raw_reset": 0, "chunks_deleted": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            key = (device_id, r["run_id"], r["epoch"])
            out["epochs"] += 1
            ext = conn.execute(
                """SELECT min(n_start) AS lo, max(n_start + n_samples) AS hi FROM chunks
                   WHERE device_id = ? AND run_id = ? AND epoch = ? AND kind = 'derived'
                     AND n_start < ? AND n_start + n_samples > ?""",
                (*key, r["n_to"], r["n_from"]),
            ).fetchone()
            n_lo = r["n_from"] if ext["lo"] is None else min(r["n_from"], ext["lo"])
            n_hi = r["n_to"] if ext["hi"] is None else max(r["n_to"], ext["hi"])
            prog = conn.execute(
                "SELECT segmented_to_n FROM raw_progress WHERE device_id = ? AND run_id = ? AND epoch = ?", key,
            ).fetchone()
            mark = prog["segmented_to_n"] if prog else None
            # The extent is redone in whole raw segments; if the last of them holds the mark,
            # the in-place path cannot apply (that segment is not behind the mark).
            seg_hi = conn.execute(
                """SELECT max(n_start + n_samples) FROM raw_segments
                   WHERE device_id = ? AND run_id = ? AND epoch = ? AND n_start < ? AND n_start + n_samples > ?""",
                (*key, n_hi, n_lo),
            ).fetchone()[0]
            tail = mark is None or seg_hi >= mark
            if tail:
                # The extent reaches the open end of the epoch: the mark moves back to n_lo and
                # everything from there on is re-derived in order, so every chunk and segment
                # from n_lo on goes, the ones between n_hi and the old mark included; leaving
                # them would let the replay derive their span a second time.
                cur = conn.execute(
                    """DELETE FROM chunks WHERE device_id = ? AND run_id = ? AND epoch = ?
                       AND kind = 'derived' AND n_start + n_samples > ?""",
                    (*key, n_lo),
                )
            else:
                cur = conn.execute(
                    """DELETE FROM chunks WHERE device_id = ? AND run_id = ? AND epoch = ?
                       AND kind = 'derived' AND n_start < ? AND n_start + n_samples > ?""",
                    (*key, n_hi, n_lo),
                )
            out["chunks_deleted"] += cur.rowcount
            if tail:
                cur = conn.execute(
                    """UPDATE raw_segments SET status = 'pending', segmented_utc_ms = NULL
                       WHERE device_id = ? AND run_id = ? AND epoch = ? AND n_start + n_samples > ?
                         AND status != 'pending'""",
                    (*key, n_lo),
                )
                epoch_start = conn.execute(
                    "SELECT min(n_start) FROM raw_segments WHERE device_id = ? AND run_id = ? AND epoch = ?",
                    key).fetchone()[0]
                if n_lo <= epoch_start:
                    conn.execute("DELETE FROM raw_progress WHERE device_id = ? AND run_id = ? AND epoch = ?", key)
                elif prog:
                    conn.execute(
                        """UPDATE raw_progress SET segmented_to_n = min(segmented_to_n, ?),
                               scanned_to_n = min(scanned_to_n, ?)
                           WHERE device_id = ? AND run_id = ? AND epoch = ?""",
                        (n_lo, n_lo, *key),
                    )
            else:
                cur = conn.execute(
                    """UPDATE raw_segments SET status = 'pending', segmented_utc_ms = NULL
                       WHERE device_id = ? AND run_id = ? AND epoch = ?
                         AND n_start + n_samples > ? AND n_start < ? AND status != 'pending'""",
                    (*key, n_lo, n_hi),
                )
            out["raw_reset"] += cur.rowcount
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
    Each row is deleted with its conditions re-checked in the same statement, so a
    `resegment` that reopened it between the select and the delete keeps it and its file.
    """
    now = now_ms() if now is None else now
    cutoff = now - retention_days * 86_400_000
    predicate = """status = 'segmented' AND start_utc_ms < ? AND received_utc_ms < ?
             AND NOT EXISTS (
                 SELECT 1 FROM chunks c
                 WHERE c.device_id = raw_segments.device_id AND c.run_id = raw_segments.run_id
                   AND c.epoch = raw_segments.epoch AND c.kind = 'derived'
                   AND c.n_start < raw_segments.n_start + raw_segments.n_samples
                   AND c.n_start + c.n_samples > raw_segments.n_start)"""
    rows = conn.execute(
        f"SELECT id, path FROM raw_segments WHERE {predicate} ORDER BY start_utc_ms", (cutoff, cutoff),
    ).fetchall()
    out = {"deleted": 0, "bytes": 0}
    for r in rows:
        cur = conn.execute(f"DELETE FROM raw_segments WHERE id = ? AND {predicate}", (r["id"], cutoff, cutoff))
        if cur.rowcount != 1:
            continue
        opus = archive_dir / r["path"]
        for p in (opus, opus.with_suffix(".json")):
            try:
                out["bytes"] += p.stat().st_size
                p.unlink()
            except FileNotFoundError:
                pass
        out["deleted"] += 1
        parent = opus.parent
        while parent != archive_dir and parent.name and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return out
