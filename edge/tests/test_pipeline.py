"""Capture pipeline driven by a fake audio source and a fake VAD (no mic)."""

import json
import struct

import numpy as np
import pytest

from conftest import make_config
from roomlog_edge import sidecar
from roomlog_edge.capture import Pipeline, Ring
from roomlog_edge.encode import Encoder
from roomlog_edge.spool import Spool
from roomlog_edge.vad import FunctionVad

RATE = 16000
W = 512
BLOCK_NS = W * 10**9 // RATE
LAT_NS = 40_000_000


class FakeEncoder:
    backend = "fake"

    def encode(self, pcm: bytes, tags: dict) -> bytes:
        return b"OggS" + struct.pack("<I", len(pcm)) + json.dumps(tags, sort_keys=True).encode() + pcm[:64]


class Source:
    """Blocks of 'speech' (loud noise) or silence with realistic clocks."""

    def __init__(self, seed=0):
        self.rng = np.random.default_rng(seed)
        self.n = 0
        self.mono = 5_000 * 10**9
        self.offset = 1_790_000_000 * 10**9 - self.mono

    def block(self, speech: bool, overflow=False, lost_blocks=0):
        self.n += lost_blocks * W
        if speech:
            pcm = (self.rng.standard_normal(W) * 6000).clip(-32000, 32000).astype(np.int16)
        else:
            pcm = np.zeros(W, dtype=np.int16)
        self.n += W
        # the callback fires once the block is complete, LAT_NS after its last
        # sample; PortAudio's currentTime - inputBufferAdcTime therefore spans
        # the block itself plus the hardware latency
        m = self.mono + self.n * 10**9 // RATE + LAT_NS
        return pcm.tobytes(), W, m, m + self.offset, BLOCK_NS + LAT_NS, overflow


def loud(window: np.ndarray) -> float:
    return 0.95 if float(np.sqrt(np.mean(window ** 2))) > 0.05 else 0.02


def make_pipeline(tmp_path, synced=True, encoder=None, **over):
    cfg = make_config(tmp_path, **over)
    spool = Spool(cfg.spool_dir)
    vad = FunctionVad(loud)
    p = Pipeline(cfg, vad, encoder or FakeEncoder(), spool, clock_synced=synced, threaded=True)
    return p, spool, vad


def feed(p, src, seconds, speech, **kw):
    for _ in range(int(seconds * RATE / W)):
        p.process_block(*src.block(speech, **kw))


def test_ring_wraps_and_reads_back():
    r = Ring(1000)
    a = np.arange(700, dtype=np.int16)
    r.write(0, a)
    r.write(700, np.arange(700, 1400, dtype=np.int16))
    assert list(r.read(600, 1400)) == list(range(600, 1400))
    with pytest.raises(ValueError):
        r.read(300, 400)  # overwritten
    with pytest.raises(ValueError):
        r.read(1300, 1500)  # not yet written


def test_speech_becomes_spooled_chunk_with_valid_sidecar(tmp_path):
    p, spool, vad = make_pipeline(tmp_path)
    src = Source()
    feed(p, src, 1.0, False)
    feed(p, src, 3.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    entries = spool.entries("pending")
    assert len(entries) == 1
    meta = entries[0].read_meta()
    assert sidecar.validate(meta) == []
    assert meta["device_id"] == "testbox" and meta["run_id"] == p.run_id
    assert meta["epoch"] == 0 and meta["cut_reason"] == "silence"
    assert not meta["discontinuity"] and not meta["clock_step"] and meta["clock_synced"]
    assert abs(meta["duration_s"] - 3.6) < 0.1  # 3 s speech + 2 * 300 ms pad
    assert meta["n_samples"] == meta["duration_s"] * RATE
    assert meta["vad"] == {"model": "silero-vad", "version": "v6.2.3", "threshold": 0.5}
    # start_utc is the sample time: 31 blocks of silence minus the 300 ms pad
    start_ns = 1_790_000_000 * 10**9 + 31 * BLOCK_NS - 300_000_000
    assert meta["start_utc"] == sidecar.format_utc(start_ns)
    # the opus bytes carry the immutable tags and the right pcm length
    raw = entries[0].opus.read_bytes()
    assert raw.startswith(b"OggS")
    (n_bytes,) = struct.unpack_from("<I", raw, 4)
    assert n_bytes == meta["n_samples"] * 2
    tags = json.loads(raw[8:raw.index(b"}") + 1])
    assert tags == {"ROOMLOG_DEVICE_ID": "testbox", "ROOMLOG_RUN_ID": p.run_id, "ROOMLOG_EPOCH": "0",
                    "ROOMLOG_N_START": str(meta["n_start"]), "ROOMLOG_EDGE_VERSION": "0.1.0"}
    assert entries[0].stem.startswith(sidecar.compact_utc(meta["start_utc"]))
    p.shutdown()


def test_overflow_cuts_chunk_and_flags_next(tmp_path):
    p, spool, vad = make_pipeline(tmp_path)
    src = Source()
    feed(p, src, 2.0, True)
    p.process_block(*src.block(True, overflow=True, lost_blocks=20))
    feed(p, src, 2.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    metas = [e.read_meta() for e in spool.entries("pending")]
    assert [m["cut_reason"] for m in metas] == ["discontinuity", "silence"]
    assert [m["epoch"] for m in metas] == [0, 1]
    assert [m["discontinuity"] for m in metas] == [False, True]
    assert vad.resets >= 1
    assert p.timeline.epoch.id == 1
    p.shutdown()


def test_unsynced_start_holds_then_releases_restamped(tmp_path):
    p, spool, vad = make_pipeline(tmp_path, synced=False)
    src = Source()
    src.offset -= 90 * 10**9  # boot clock 90 s slow
    feed(p, src, 2.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    held = spool.entries("unsynced")
    assert len(held) == 1 and spool.entries("pending") == []
    stale = held[0].read_meta()
    assert stale["clock_synced"] is False
    # NTP arrives: the clock steps forward 90 s and the timeline sees it
    src.offset += 90 * 10**9
    p.process_block(*src.block(False))
    p.set_synced(True)
    assert spool.entries("unsynced") == []
    (e,) = spool.entries("pending")
    meta = e.read_meta()
    assert meta["clock_synced"] is True and meta["clock_step"] is True
    assert meta["sha256"] == stale["sha256"]
    true_start = 1_790_000_000 * 10**9  # speech began at n=0, no pad before the origin
    assert meta["start_utc"] == sidecar.format_utc(true_start)
    assert e.stem.startswith(sidecar.compact_utc(meta["start_utc"]))
    # later chunks go straight to pending, synced
    feed(p, src, 1.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    assert len(spool.entries("pending")) == 2
    assert spool.entries("pending")[1].read_meta()["clock_synced"] is True
    p.shutdown()


def test_unsynced_on_shutdown_released_as_is(tmp_path):
    p, spool, vad = make_pipeline(tmp_path, synced=False)
    src = Source()
    feed(p, src, 1.0, True)
    p.shutdown()
    (e,) = spool.entries("pending")
    meta = e.read_meta()
    assert meta["clock_synced"] is False and meta["cut_reason"] == "shutdown"
    assert spool.entries("unsynced") == []


def test_clock_step_during_open_chunk(tmp_path):
    p, spool, vad = make_pipeline(tmp_path)
    src = Source()
    feed(p, src, 1.0, True)
    src.offset += 3600 * 10**9
    feed(p, src, 1.0, True)
    feed(p, src, 2.0, False)
    feed(p, src, 1.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    metas = [e.read_meta() for e in spool.entries("pending")]
    assert [m["clock_step"] for m in metas] == [True, False]
    assert [m["epoch"] for m in metas] == [0, 0]
    # post-step mapping: speech began at n=0 → 1_790_000_000 + 3600 s
    assert metas[0]["start_utc"] == sidecar.format_utc((1_790_000_000 + 3600) * 10**9)
    p.shutdown()


def test_disk_guard_drops_and_counts(tmp_path):
    p, spool, vad = make_pipeline(tmp_path)
    spool.max_bytes = 10
    (spool.dir("failed") / "old.opus").write_bytes(b"x" * 64)  # already above the mark
    src = Source()
    feed(p, src, 1.0, True)
    feed(p, src, 2.0, False)
    p.flush()
    assert spool.entries("pending") == [] and p.chunks_dropped == 1
    p.shutdown()


def test_status_and_rms(tmp_path):
    p, spool, vad = make_pipeline(tmp_path)
    src = Source()
    feed(p, src, 0.5, False)
    assert p.last_rms_dbfs == -120.0
    feed(p, src, 0.5, True)
    assert -30 < p.last_rms_dbfs < 0
    st = p.status()
    assert st["epoch"] == 0 and st["blocks"] == 30 and st["clock_synced"] and st["chunk_open"]
    p.shutdown()


def test_real_encoder_end_to_end(tmp_path, encoder_available):
    from roomlog_edge import ogg

    p, spool, vad = make_pipeline(tmp_path, encoder=Encoder("auto"))
    src = Source()
    feed(p, src, 1.5, True)
    feed(p, src, 2.0, False)
    p.flush()
    (e,) = spool.entries("pending")
    meta = e.read_meta()
    info = ogg.parse(e.opus.read_bytes())
    assert info.tags["ROOMLOG_RUN_ID"] == p.run_id and info.tags["ROOMLOG_N_START"] == str(meta["n_start"])
    assert abs(info.duration_s - meta["duration_s"]) < 0.1
    p.shutdown()
