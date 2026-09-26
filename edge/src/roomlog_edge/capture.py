"""Capture pipeline: mic → timeline → VAD/chunker → Opus + sidecar → spool.

:class:`Pipeline` is the I/O-light core (it writes to the spool and runs the
encoder subprocess in one worker thread, nothing else) and is driven by
:meth:`Pipeline.process_block`; tests feed it synthetic blocks with a fake VAD.
:func:`run_capture` wraps it with the PortAudio stream (``sounddevice`` is
imported there, lazily), the sync gate, systemd notify and the status file.
"""

from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import __version__, clocksync, sdnotify, sidecar, vad as vadmod
from .chunker import Chunk, Chunker
from .config import Config
from .encode import Encoder, roomlog_tags
from .spool import Spool
from .status import write_status
from .timeline import NS, Block, Timeline

MS = 1_000_000

log = logging.getLogger("roomlog.capture")

EXIT_STREAM_ERROR = 1
EXIT_MIC_DEAD = 3


class Ring:
    """Ring buffer of int16 samples indexed by absolute sample number."""

    def __init__(self, size: int):
        self.size = size
        self.buf = np.zeros(size, dtype=np.int16)
        self.n_end = 0  # samples written so far; the buffer holds [n_end - size, n_end)

    def write(self, n_start: int, samples: np.ndarray) -> None:
        if n_start != self.n_end:
            raise ValueError("ring writes must be contiguous")
        n = len(samples)
        if n > self.size:
            samples = samples[-self.size:]
            n_start += len(samples) - n
            n = self.size
        pos = n_start % self.size
        first = min(n, self.size - pos)
        self.buf[pos:pos + first] = samples[:first]
        if first < n:
            self.buf[:n - first] = samples[first:]
        self.n_end = n_start + n

    def read(self, n_start: int, n_end: int) -> np.ndarray:
        if n_end > self.n_end or n_start < self.n_end - self.size or n_end - n_start > self.size:
            raise ValueError(f"[{n_start}, {n_end}) is not in the ring (holds [{self.n_end - self.size}, {self.n_end}))")
        pos = n_start % self.size
        n = n_end - n_start
        first = min(n, self.size - pos)
        out = np.empty(n, dtype=np.int16)
        out[:first] = self.buf[pos:pos + first]
        if first < n:
            out[first:] = self.buf[:n - first]
        return out


@dataclass
class _Job:
    chunk: Chunk
    epoch: int
    pcm: bytes
    tags: dict
    seq: int = 0


@dataclass
class _Encoded:
    job: _Job
    opus: bytes | None
    error: str | None


class Pipeline:
    def __init__(self, cfg: Config, vad: vadmod.Vad, encoder: Encoder, spool: Spool,
                 clock_synced: bool = True, run_id: str | None = None, threaded: bool = True,
                 clocks=None):
        self.cfg = cfg
        self.vad = vad
        self.encoder = encoder
        self.spool = spool
        self.run_id = run_id or str(uuid.uuid4())
        self.rate = cfg.audio.sample_rate
        self.window = vadmod.WINDOW
        # clocks() -> (real_ns, mono_ns); read when re-stamping held chunks
        self.clocks = clocks or (lambda: (time.time_ns(), time.monotonic_ns()))
        t = cfg.timeline
        self.timeline = Timeline(rate=self.rate, lateness_limit_ns=t.lateness_limit_ms * MS,
                                 late_blocks=t.late_blocks, step_limit_ns=t.step_limit_ms * MS,
                                 reref_interval_ns=int(t.reref_interval_s * NS))
        c = cfg.chunker
        self.chunker = Chunker.from_ms(rate=self.rate, window=self.window, threshold=cfg.vad.threshold,
                                       neg_threshold=cfg.vad.neg_threshold, pad_ms=c.pad_ms,
                                       min_silence_ms=c.min_silence_ms, min_speech_ms=c.min_speech_ms,
                                       max_chunk_s=c.max_chunk_s, min_pause_ms=c.min_pause_ms)
        self.ring = Ring(int(c.ring_s * self.rate))
        self.clock_synced = clock_synced
        self.vad_info = vadmod.info(cfg.vad.threshold)
        # windowing state (windows are aligned to the epoch origin)
        self._origin = 0
        self._epoch = 0
        self._pending = np.zeros(0, dtype=np.int16)
        self._next_index = 0
        # samples whose timeline mapping is still needed: in-flight encode
        # jobs and chunks held in unsynced/ from this run
        self._inflight: dict[int, int] = {}  # job seq -> n_start
        self._seq = 0
        self._held: dict[str, tuple[int, int]] = {}  # sha8 -> (n_start, step_count at hold)
        # A silent gap is confirmed only after several late blocks. Keep chunks
        # cut during that short interval out of the encoder until it resolves.
        self._deferred: list[tuple[Chunk, int]] = []
        # stats
        self.blocks = 0
        self.chunks_written = 0
        self.chunks_dropped = 0
        self.chunks_failed = 0
        self.last_rms_dbfs: float | None = None
        self.last_prob = 0.0
        self.last_block_mono_ns: int | None = None
        # encoder worker
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._done: queue.Queue[_Encoded] = queue.Queue()
        self._threaded = threaded
        if threaded:
            self._worker = threading.Thread(target=self._encode_loop, name="roomlog-encode", daemon=True)
            self._worker.start()

    # -- input -------------------------------------------------------------

    def process_block(self, pcm: bytes, n_frames: int, mono_ns: int, real_ns: int,
                      adc_latency_ns: int, input_overflow: bool = False) -> None:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if len(samples) != n_frames:
            n_frames = len(samples)
        res = self.timeline.feed(Block(n_frames, mono_ns, real_ns, adc_latency_ns, input_overflow))
        self.blocks += 1
        self.last_block_mono_ns = mono_ns
        if n_frames:
            f = samples.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(f * f)))
            self.last_rms_dbfs = 20 * np.log10(rms) if rms > 0 else -120.0
        if res.clock_step_ns is not None:
            log.warning("clock step of %.3f s at n=%d", res.clock_step_ns / NS, res.n_start)
        self.ring.write(res.n_start, samples)
        if res.new_epoch:
            self._submit_deferred(res.epoch_start_n if res.epoch > 0 else None)
            if res.epoch > 0:
                log.warning("sample loss: new epoch %d starts at n=%d (%s, lateness %.1f ms)",
                            res.epoch, res.epoch_start_n,
                            "xrun" if input_overflow else "silent", res.lateness_ns / 1e6)
            self._start_epoch(res.epoch_start_n, res.epoch)
            if res.epoch_start_n < res.n_start:
                # silent loss was decided a few blocks late: the blocks since
                # the gap belong to the new epoch, re-run them through the VAD
                samples = self.ring.read(res.epoch_start_n, res.n_end)
        elif res.lateness_ns <= self.timeline.lateness_limit_ns:
            self._submit_deferred()
        defer_cuts = not res.new_epoch and res.lateness_ns > self.timeline.lateness_limit_ns
        self._pending = np.concatenate([self._pending, samples]) if len(self._pending) else samples
        while len(self._pending) >= self.window:
            win = self._pending[:self.window]
            self._pending = self._pending[self.window:]
            p = self.vad(vadmod.pcm_to_float(win))
            self.last_prob = p
            for chunk in self.chunker.feed(self._next_index, p):
                if defer_cuts:
                    self._deferred.append((chunk, self._epoch))
                else:
                    self._submit(chunk)
            self._next_index += 1
        self.drain_encoded()
        self.timeline.retain_from_n = self._retain_from_n()

    def _retain_from_n(self) -> int:
        n = self.ring.n_end - self.ring.size
        if self._inflight:
            n = min(n, min(self._inflight.values()))
        if self._held:
            n = min(n, min(h[0] for h in self._held.values()))
        return max(n, 0)

    def _start_epoch(self, n_start: int, epoch: int) -> None:
        if epoch > 0:
            # close the old chunk before the first lost sample; never span a gap
            for chunk in self.chunker.cut("discontinuity", n_end=n_start):
                self._submit(chunk, epoch=epoch - 1)
        self.chunker.reset(n_start)
        self.vad.reset()
        self._pending = np.zeros(0, dtype=np.int16)
        self._next_index = 0
        self._origin = n_start
        self._epoch = epoch

    def _submit_deferred(self, gap_n: int | None = None) -> None:
        """Commit tentative cuts, clipping them if the late blocks revealed a gap."""
        for chunk, epoch in self._deferred:
            if gap_n is not None and chunk.n_end > gap_n:
                chunk = Chunk(chunk.n_start, gap_n, chunk.cut_reason, chunk.discontinuity)
            if chunk.n_end > chunk.n_start:
                self._submit(chunk, epoch=epoch)
        self._deferred.clear()

    def _submit(self, chunk: Chunk, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        try:
            pcm = self.ring.read(chunk.n_start, chunk.n_end).tobytes()
        except ValueError as e:
            log.error("chunk lost, ring too small: %s", e)
            self.chunks_dropped += 1
            return
        tags = roomlog_tags(self.cfg.device_id, self.run_id, epoch, chunk.n_start, __version__)
        self._seq += 1
        job = _Job(chunk, epoch, pcm, tags, self._seq)
        self._inflight[job.seq] = chunk.n_start
        if self._threaded:
            self._jobs.put(job)
        else:
            self._done.put(self._encode(job))

    # -- encoding ----------------------------------------------------------

    def _encode(self, job: _Job) -> _Encoded:
        try:
            return _Encoded(job, self.encoder.encode(job.pcm, job.tags), None)
        except Exception as e:  # noqa: BLE001 - the worker must survive
            return _Encoded(job, None, str(e))

    def _encode_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                return
            self._done.put(self._encode(job))
            self._jobs.task_done()

    def drain_encoded(self) -> int:
        n = 0
        while True:
            try:
                enc = self._done.get_nowait()
            except queue.Empty:
                return n
            self._store(enc)
            n += 1

    def flush(self) -> None:
        """Wait for the encoder to finish everything submitted, then store it."""
        if self._threaded:
            self._jobs.join()
        self.drain_encoded()

    def _store(self, enc: _Encoded) -> None:
        job = enc.job
        self._inflight.pop(job.seq, None)
        if enc.opus is None:
            log.error("encode failed for n=%d: %s", job.chunk.n_start, enc.error)
            self.chunks_failed += 1
            return
        if not self.spool.disk_ok():
            log.error("spool full or disk low: dropping chunk n=%d", job.chunk.n_start)
            self.chunks_dropped += 1
            return
        meta = self._stamp(job.chunk, job.epoch, sha256=None, clock_synced=self.clock_synced)
        dest = "pending" if self.clock_synced else "unsynced"
        entry = self.spool.write(enc.opus, meta, dest)
        if dest == "unsynced":
            self._held[entry.stem[-8:]] = (job.chunk.n_start, self.timeline.step_count)
        self.chunks_written += 1

    def _stamp(self, chunk: Chunk, epoch: int, sha256: str | None, clock_synced: bool) -> dict:
        return sidecar.build(
            device_id=self.cfg.device_id,
            sha256=sha256 or "0" * 64,
            utc_ns=self.timeline.utc_ns(chunk.n_start, epoch),
            n_start=chunk.n_start,
            n_samples=chunk.n_samples,
            run_id=self.run_id,
            epoch=epoch,
            discontinuity=chunk.discontinuity,
            clock_step=self.timeline.stepped_in(chunk.n_start, chunk.n_end),
            clock_synced=clock_synced,
            cut_reason=chunk.cut_reason,
            vad=self.vad_info,
            edge_version=__version__,
            sample_rate=self.rate,
        )

    # -- clock sync --------------------------------------------------------

    def set_synced(self, synced: bool) -> None:
        if synced and not self.clock_synced:
            # The sync flag may have flipped after the last callback's clocks
            # were read; look at the clocks now so the step is applied before
            # the held chunks are re-stamped.
            real_ns, mono_ns = self.clocks()
            delta = self.timeline.observe_offset(real_ns, mono_ns)
            if delta is not None:
                log.warning("clock step of %.3f s seen at sync", delta / NS)
            self.clock_synced = True
            n = self.release_unsynced(restamp=True)
            log.info("clock synced: released %d held chunks", n)
        elif not synced and self.clock_synced:
            log.warning("clock reported unsynced")
            self.clock_synced = False

    def release_unsynced(self, restamp: bool) -> int:
        """Move ``unsynced/`` into ``pending/``. With ``restamp`` the chunks of
        this run get ``start_utc`` recomputed through the corrected mapping and
        ``clock_synced: true``; others (older runs, or on exit) move as-is."""
        n = 0
        for entry in self.spool.entries("unsynced"):
            try:
                meta = entry.read_meta()
            except (OSError, ValueError):
                self.spool.move(entry, "failed")
                continue
            held = self._held.pop(entry.stem[-8:], None)
            if (restamp and meta.get("run_id") == self.run_id and meta.get("epoch") in self.timeline.epochs
                    and isinstance(meta.get("n_start"), int)):
                meta["start_utc"] = sidecar.format_utc(self.timeline.utc_ns(meta["n_start"], meta["epoch"]))
                meta["clock_synced"] = True
                if held is not None and self.timeline.step_count > held[1]:
                    meta["clock_step"] = True  # a step was applied to this start_utc
                self.spool.rewrite_meta(entry, meta, "pending")
            else:
                self.spool.move(entry, "pending")
            n += 1
        self.timeline.retain_from_n = self._retain_from_n()
        return n

    # -- shutdown ----------------------------------------------------------

    def shutdown(self) -> None:
        self._submit_deferred()
        for chunk in self.chunker.cut("shutdown"):
            self._submit(chunk)
        self.flush()
        if self._threaded:
            self._jobs.put(None)
            self._worker.join(timeout=5)
        self.release_unsynced(restamp=False)

    def status(self) -> dict:
        ep = self.timeline.epoch
        return {
            "run_id": self.run_id,
            "epoch": ep.id if ep else None,
            "n": self.timeline.n,
            "blocks": self.blocks,
            "last_block_mono_ns": self.last_block_mono_ns,
            "last_frame_age_s": (time.monotonic_ns() - self.last_block_mono_ns) / NS if self.last_block_mono_ns else None,
            "rms_dbfs": self.last_rms_dbfs,
            "vad_prob": self.last_prob,
            "clock_synced": self.clock_synced,
            "clock_steps_n": self.timeline.last_step_n,
            "chunks_written": self.chunks_written,
            "chunks_dropped": self.chunks_dropped,
            "chunks_failed": self.chunks_failed,
            "encode_queue": self._jobs.qsize(),
            "chunk_open": self.chunker.open,
        }


# -- PortAudio source --------------------------------------------------------


class SoundDeviceSource:
    """RawInputStream whose callback only enqueues a block record."""

    def __init__(self, device, sample_rate: int, blocksize: int):
        import sounddevice as sd  # lazy: needs libportaudio

        self.q: queue.Queue = queue.Queue()
        self.error: str | None = None
        self.sd = sd
        self.stream = sd.RawInputStream(samplerate=sample_rate, blocksize=blocksize, device=device,
                                        channels=1, dtype="int16", callback=self._callback,
                                        finished_callback=self._finished)

    def _callback(self, indata, frames, t, status):
        m = time.monotonic_ns()
        r = time.time_ns()
        adc = t.inputBufferAdcTime
        lat = (t.currentTime - adc) if adc > 0 else 0.0
        if lat < 0:
            lat = 0.0
        self.q.put((bytes(indata), frames, m, r, int(lat * NS), bool(status.input_overflow)))

    def _finished(self):
        if self.stream.closed or not self.stream.active:
            self.error = self.error or "stream finished"
        self.q.put(None)

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


def wait_for_sync(cfg: Config, stop: threading.Event) -> bool:
    deadline = time.monotonic() + cfg.clock.sync_wait_s
    while not stop.is_set():
        state = clocksync.ntp_synchronized()
        if state:
            return True
        if time.monotonic() >= deadline:
            log.warning("clock not synced after %.0f s, capturing unsynced", cfg.clock.sync_wait_s)
            return False
        sdnotify.status("waiting for NTP sync")
        stop.wait(min(cfg.clock.sync_poll_s, max(0.0, deadline - time.monotonic())))
    return False


def run_capture(cfg: Config) -> int:
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("signal %d, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    spool = Spool(cfg.spool_dir, cfg.spool.max_bytes, cfg.spool.min_free_fraction)
    n = spool.cleanup_tmp()
    if n:
        log.info("removed %d leftover tmp files", n)
    try:
        vad = vadmod.SileroVad(cfg.model_path, cfg.audio.sample_rate, cfg.model_sha256)
    except Exception as e:  # noqa: BLE001
        log.error("cannot load VAD model %s: %s", cfg.model_path, e)
        return EXIT_STREAM_ERROR
    encoder = Encoder(cfg.encoder.backend, cfg.encoder.bitrate_kbps, cfg.audio.sample_rate)
    log.info("encoder backend: %s", encoder.backend)

    synced = wait_for_sync(cfg, stop)
    if stop.is_set():
        return 0
    pipeline = Pipeline(cfg, vad, encoder, spool, clock_synced=synced)
    pipeline.release_unsynced(restamp=False)  # leftovers from an earlier run
    log.info("run_id %s, clock_synced=%s", pipeline.run_id, synced)

    try:
        source = SoundDeviceSource(cfg.audio.device, cfg.audio.sample_rate, cfg.audio.blocksize)
        source.start()
    except Exception as e:  # noqa: BLE001
        log.error("cannot open input device %r: %s", cfg.audio.device, e)
        return EXIT_STREAM_ERROR
    sdnotify.ready()
    log.info("capturing from %r", cfg.audio.device)

    status_every = 5.0
    sync_every = cfg.clock.sync_poll_s
    last_status = last_sync = time.monotonic()
    zero_s = 0.0
    rc = 0
    try:
        while not stop.is_set():
            try:
                item = source.q.get(timeout=1.0)
            except queue.Empty:
                item = ...
            if item is None:
                log.error("input stream stopped: %s", source.error)
                rc = EXIT_STREAM_ERROR
                break
            if item is not ...:
                pcm, frames, m, r, lat, overflow = item
                pipeline.process_block(pcm, frames, m, r, lat, overflow)
                # Any arriving frame feeds the watchdog; exactly-zero audio is
                # handled by the exit-3 path below, which shuts down cleanly
                # (a watchdog SIGABRT would skip the shutdown cut).
                if frames:
                    sdnotify.watchdog()
                if frames and pcm.count(b"\x00") == len(pcm):
                    zero_s += frames / cfg.audio.sample_rate
                    if zero_s >= cfg.audio.zero_exit_s:
                        log.error("mic dead: %.0f s of exactly-zero samples", zero_s)
                        rc = EXIT_MIC_DEAD
                        break
                else:
                    zero_s = 0.0
            now = time.monotonic()
            if now - last_sync >= sync_every:
                last_sync = now
                state = clocksync.ntp_synchronized()
                if state is not None:
                    pipeline.set_synced(state)
            if now - last_status >= status_every:
                last_status = now
                pipeline.drain_encoded()
                write_status(cfg.capture_status, pipeline.status())
    finally:
        sdnotify.stopping()
        source.stop()
        pipeline.shutdown()
        write_status(cfg.capture_status, {**pipeline.status(), "stopped": True})
    return rc
