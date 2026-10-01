"""Segmenter (ADR 0005): synthetic raw segments through an energy VAD and the real chunker."""

from __future__ import annotations

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server.audio import decode_opus
from roomlog_server.backends import Router
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.queries import search
from roomlog_server.segmenter import (
    EnergyVad, RawAudio, Segmenter, pcm_sha256, purge_raw, reset_segmentation, samples_to_ms,
)
from roomlog_server.worker import Chunk, Worker

from conftest import RATE, RAW_T0, SEGMENT, RawSegments, ffmpeg_required, make_config, pattern_pcm

W = 512
PAD = 4800
DAY = 86_400_000


@pytest.fixture(autouse=True)
def _need_ffmpeg():
    ffmpeg_required()


def make_segmenter(cfg, conn, now_ms: int, **kw) -> Segmenter:
    return Segmenter(conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"},
                     now=lambda: now_ms, **kw)


def derived(conn, device_id: str | None = None) -> list[dict]:
    where = "WHERE kind = 'derived'" + (" AND device_id = ?" if device_id else "")
    rows = conn.execute(f"SELECT * FROM chunks {where} ORDER BY n_start", (device_id,) if device_id else ())
    return [dict(r) for r in rows]


def raw_status(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM raw_segments ORDER BY n_start")]


def window_start(sample: int) -> int:
    """The chunker sees speech from the start of the 512-sample window holding `sample`."""
    return (sample // W) * W


def window_after(sample: int) -> int:
    """The first window start at or after `sample` (where silence is first seen)."""
    return -(-sample // W) * W


# ---------------------------------------------------------------- basics


def test_chunk_spanning_segment_boundary_is_exact_and_idempotent(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    pcm = pattern_pcm([("s", 25), ("t", 15), ("s", 35)])
    metas = fx.store_all(pcm, last_cut="shutdown")
    assert [m["n_samples"] for m in metas] == [SEGMENT, SEGMENT, 240_000]

    seg = make_segmenter(cfg, conn, RAW_T0 + 100_000)
    res = seg.run_once()
    assert (res.epochs, res.chunks, res.errors) == (1, 1, [])
    chunks = derived(conn)
    assert len(chunks) == 1
    c = chunks[0]
    n_start = window_start(25 * RATE) - PAD
    n_end = c["n_start"] + c["n_samples"]
    assert c["n_start"] == n_start
    # the codec's tail ringing may keep one more window above the energy gate
    assert n_end in (window_after(40 * RATE) + PAD, window_after(40 * RATE) + W + PAD)
    assert c["n_start"] < SEGMENT < n_end  # spans the 30 s seam
    assert c["kind"] == "derived" and c["path"] is None and c["status"] == "pending"
    assert c["start_utc_ms"] == RAW_T0 + n_start * 1000 // RATE  # exact: n_start is a multiple of 16
    assert c["end_utc_ms"] - c["start_utc_ms"] == c["duration_ms"] == samples_to_ms(n_end - n_start)
    assert c["run_id"] == fx.run_id and c["epoch"] == 0
    assert c["discontinuity"] == 0 and c["clock_synced"] == 1
    import json
    meta = json.loads(c["meta_json"])
    assert meta["kind"] == "derived" and meta["cut_reason"] == "silence"
    assert meta["vad"] == {"model": "energy"}
    assert meta["derived_from"] == [metas[0]["sha256"], metas[1]["sha256"]]
    assert meta["start_utc"] == "2026-09-26T13:00:24.692Z"

    # the derived audio is exactly the slice of the decoded raw stream, and its sha is the pcm sha
    stream = np.concatenate([decode_opus(cfg.archive_dir / m_path) for m_path in
                             [r[0] for r in conn.execute("SELECT path FROM raw_segments ORDER BY n_start")]])
    assert len(stream) == len(pcm)
    expected = stream[n_start:n_end]
    worker = Worker(cfg, conn, Router([FakeBackend()]))
    row = conn.execute("SELECT * FROM chunks WHERE id = ?", (c["id"],)).fetchone()
    got = worker.audio_for(Chunk.from_row(row))
    assert got.dtype == np.float32 and np.array_equal(got, expected)
    assert c["sha256"] == pcm_sha256(expected)
    # and the tone really is in there: the decoded slice correlates with the source
    src = pcm[n_start:n_end].astype(np.float32) / 32768.0
    assert np.corrcoef(src[8000:-8000], got[8000:-8000])[0, 1] > 0.9

    assert raw_status(conn) == ["segmented"] * 3
    prog = conn.execute("SELECT * FROM raw_progress").fetchone()
    assert (prog["segmented_to_n"], prog["scanned_to_n"]) == (len(pcm), len(pcm))

    # a second pass has nothing to do and changes nothing
    res2 = make_segmenter(cfg, conn, RAW_T0 + 200_000).run_once()
    assert (res2.epochs, res2.chunks) == (0, 0)
    assert derived(conn) == chunks
    conn.close()


def test_waits_for_successor_then_idle_tail_gets_shutdown_cut(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    pcm = pattern_pcm([("s", 50), ("t", 10)])
    fx.store_all(pcm, last_cut="cap")  # two full segments, the next one still on the phone
    received = fx.received_utc_ms

    res = make_segmenter(cfg, conn, received + 10_000).run_once()
    assert (res.epochs, res.chunks) == (1, 0)  # segment 1 scanned (silence); segment 2 waits
    # the last pad before the seam stays open so speech right after it keeps its pre-roll
    assert conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] == window_start(SEGMENT) - PAD
    assert raw_status(conn) == ["pending", "pending"]

    res = make_segmenter(cfg, conn, received + 60_000).run_once()
    assert (res.epochs, res.chunks) == (0, 0)  # nothing new arrived: no decode, no work

    res = make_segmenter(cfg, conn, received + 121_000).run_once()
    assert (res.epochs, res.chunks) == (1, 1)
    c = derived(conn)[0]
    assert c["n_start"] == window_start(50 * RATE) - PAD
    assert c["n_start"] + c["n_samples"] == 2 * SEGMENT
    import json
    assert json.loads(c["meta_json"])["cut_reason"] == "shutdown"
    assert raw_status(conn) == ["segmented", "segmented"]
    conn.close()


def test_epoch_ending_segment_is_processed_without_waiting(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    pcm = pattern_pcm([("s", 30), ("t", 3)])
    fx.store_all(pcm, last_cut="discontinuity")
    res = make_segmenter(cfg, conn, fx.received_utc_ms + 1000).run_once()
    assert res.chunks == 1
    import json
    assert json.loads(derived(conn)[0]["meta_json"])["cut_reason"] == "discontinuity"
    conn.close()


def test_contiguity_break_starts_fresh_at_its_own_origin(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store(0, pattern_pcm([("s", 30)]), "cap")
    # segment at 480000 never arrives; the chain restarts at 960000 with speech from its first sample
    late = RawSegments(cfg, conn, run_id=fx.run_id, epoch_start_n=2 * SEGMENT,
                       start_utc_ms=RAW_T0 + 60_000, received_utc_ms=fx.received_utc_ms)
    late.store_all(pattern_pcm([("t", 5), ("s", 25), ("s", 30)]), last_cut="shutdown")

    res = make_segmenter(cfg, conn, fx.received_utc_ms + 200_000).run_once()
    assert (res.epochs, res.chunks) == (1, 1)
    c = derived(conn)[0]
    assert c["n_start"] == 2 * SEGMENT  # no padding before the chain's origin
    assert c["start_utc_ms"] == RAW_T0 + 60_000
    assert raw_status(conn) == ["segmented"] * 3
    conn.close()


def test_incremental_arrival_matches_one_shot(tmp_path):
    """Feeding segments one at a time (warm-up, mark, clipping) yields the one-shot chunk set."""
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    pattern = [("s", 10), ("t", 20), ("s", 5), ("t", 40), ("s", 20), ("t", 3), ("s", 52)]  # 150 s
    pcm = pattern_pcm(pattern)

    one = RawSegments(cfg, conn, device_id="one")
    one.store_all(pcm, last_cut="shutdown")
    make_segmenter(cfg, conn, one.received_utc_ms + 1000).run_once()
    one_shot = [(c["n_start"], c["n_samples"]) for c in derived(conn, "one")]
    assert len(one_shot) >= 4  # 20 s, 30 s cap + 10 s continuation, 3 s

    # a different amplitude: identical PCM would hash to the same sha and be deduplicated
    inc = RawSegments(cfg, conn, device_id="inc", run_id=one.run_id)
    for n, piece, reason in inc.plan(pattern_pcm(pattern, amp=6000), last_cut="shutdown"):
        inc.store(n, piece, reason)
        make_segmenter(cfg, conn, inc.received_utc_ms + 1000).run_once()
    incremental = [(c["n_start"], c["n_samples"]) for c in derived(conn, "inc")]
    assert incremental == one_shot
    assert raw_status(conn) == ["segmented"] * 10
    conn.close()


def test_discontinuity_and_clock_synced_come_from_the_raw_segment(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn, epoch=1, epoch_start_n=1_000_000, discontinuity=True, clock_synced=False)
    fx.store_all(pattern_pcm([("t", 5), ("s", 5), ("t", 5), ("s", 20)]), last_cut="shutdown")
    res = make_segmenter(cfg, conn, fx.received_utc_ms + 1000).run_once()
    assert res.chunks == 2
    a, b = derived(conn)
    assert (a["discontinuity"], b["discontinuity"]) == (1, 0)
    assert (a["clock_synced"], b["clock_synced"]) == (0, 0)
    assert a["n_start"] == 1_000_000 and a["epoch"] == 1
    conn.close()


def test_late_segment_filling_a_gap_behind_the_mark_is_segmented_alone(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    plan = fx.plan(pattern_pcm([("s", 30), ("s", 10), ("t", 10), ("s", 10), ("s", 30)]), last_cut="shutdown")
    fx.store(*plan[0])
    fx.store(*plan[2])
    make_segmenter(cfg, conn, fx.received_utc_ms + 1000).run_once()
    assert derived(conn) == []
    # a young gap: the first segment waits for its successor, and the last one, though it ends
    # the epoch, waits behind it (the mark is per epoch and must not pass an open chain)
    assert raw_status(conn) == ["pending", "pending"]
    assert conn.execute("SELECT count(*) FROM raw_progress").fetchone()[0] == 0
    make_segmenter(cfg, conn, fx.received_utc_ms + 121_000).run_once()
    # the gap aged past raw_idle_s: both chains are closed, the mark is at the epoch's end
    assert raw_status(conn) == ["segmented", "segmented"]
    assert conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] == 3 * SEGMENT
    fx.store(*plan[1])  # the middle one, with the speech, arrives late
    res = make_segmenter(cfg, conn, fx.received_utc_ms + 122_000).run_once()
    assert res.chunks == 1
    c = derived(conn)[0]
    assert SEGMENT <= c["n_start"] and c["n_start"] + c["n_samples"] <= 2 * SEGMENT
    assert raw_status(conn) == ["segmented"] * 3
    conn.close()


def test_missing_model_is_reported_not_raised(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(pattern_pcm([("t", 5)]), last_cut="shutdown")

    def factory():
        raise FileNotFoundError("no model")

    seg = Segmenter(conn, cfg.archive_dir, factory, now=lambda: fx.received_utc_ms + 1000)
    res = seg.run_once()
    assert res.chunks == 0 and len(res.errors) == 1 and "no model" in res.errors[0]
    assert raw_status(conn) == ["pending"]
    conn.close()


# ---------------------------------------------------------------- worker


def test_worker_transcribes_derived_chunk_with_absolute_times(tmp_path):
    cfg = make_config(tmp_path, dictation_hold_s=0)  # the clock here is 1 s past the audio
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(pattern_pcm([("s", 25), ("t", 15), ("s", 35)]), last_cut="shutdown")
    now = fx.received_utc_ms + 1000
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0), now=lambda: now,
                    segmenter=make_segmenter(cfg, conn, now))
    res = worker.run_once()
    assert (res.segmented_epochs, res.segmented_chunks) == (1, 1)
    assert (res.claimed, res.done, res.failed) == (1, 1, 0)
    c = derived(conn)[0]
    assert c["status"] == "done" and c["model_id"] == "fake-model"
    segs = [dict(r) for r in conn.execute("SELECT * FROM segments ORDER BY idx")]
    assert len(segs) == 1
    assert "ord00" in segs[0]["text"]
    # the fake backend reports the tone region; it starts one pad (300 ms) into the chunk
    tone_start_ms = RAW_T0 + 25_000
    assert abs(segs[0]["start_utc_ms"] - tone_start_ms) <= 50
    assert abs(segs[0]["end_utc_ms"] - (RAW_T0 + 40_000)) <= 50
    assert segs[0]["start_utc_ms"] - c["start_utc_ms"] == segs[0]["offset_ms"]
    hits = search(conn, "ord00")
    assert len(hits) == 1 and hits[0]["device_id"] == "s22"
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    # the batch is idempotent for the segmenter and leaves nothing pending
    res2 = worker.run_once()
    assert (res2.segmented_chunks, res2.claimed) == (0, 0)
    conn.close()


def test_worker_marks_derived_chunk_failed_when_raw_audio_is_gone(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(pattern_pcm([("t", 5), ("s", 25)]), last_cut="shutdown")
    now = fx.received_utc_ms + 1000
    make_segmenter(cfg, conn, now).run_once()
    conn.execute("DELETE FROM raw_segments")
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0), now=lambda: now)
    res = worker.run_once()
    assert (res.claimed, res.failed) == (1, 1)
    assert derived(conn)[0]["error"].startswith("decode:")
    conn.close()


# ---------------------------------------------------------------- resegment and purge


def test_reset_segmentation_rederives_the_same_chunks(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(pattern_pcm([("s", 25), ("t", 15), ("s", 5), ("t", 5), ("s", 25)]), last_cut="shutdown")
    now = fx.received_utc_ms + 1000
    make_segmenter(cfg, conn, now).run_once()
    before = derived(conn)
    assert len(before) == 2
    conn.execute("UPDATE chunks SET status = 'done'")
    conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) "
                 "VALUES (?, 's22', 0, 0, 1, 0, 'x', 'm')", (before[0]["id"],))

    out = reset_segmentation(conn, "s22")
    assert out == {"epochs": 1, "raw_reset": 3, "chunks_deleted": 2}
    assert derived(conn) == [] and conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
    assert raw_status(conn) == ["pending"] * 3
    assert conn.execute("SELECT count(*) FROM raw_progress").fetchone()[0] == 0
    make_segmenter(cfg, conn, now).run_once()
    after = derived(conn)
    assert [(c["sha256"], c["n_start"], c["n_samples"]) for c in after] == \
        [(c["sha256"], c["n_start"], c["n_samples"]) for c in before]
    assert all(c["status"] == "pending" for c in after)

    # a range that starts inside the epoch: the straddling chunk is re-derived whole
    out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + 30_000)
    assert out["chunks_deleted"] == 2  # both chunks end after 30 s
    # the mark moves back to the straddling chunk's start, inside segment 0, which reopens too
    # and is counted: `raw=` is what was actually reset, not what the range selected
    assert out["raw_reset"] == 3
    prog = conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()
    assert prog["segmented_to_n"] == before[0]["n_start"]
    assert raw_status(conn) == ["pending", "pending", "pending"]
    make_segmenter(cfg, conn, now).run_once()
    assert [c["sha256"] for c in derived(conn)] == [c["sha256"] for c in before]
    # another device is untouched
    assert reset_segmentation(conn, "nobody") == {"epochs": 0, "raw_reset": 0, "chunks_deleted": 0}
    conn.close()


def test_purge_raw_deletes_only_old_speech_free_segmented_segments(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    old_start = RAW_T0 - 40 * DAY
    old = RawSegments(cfg, conn, start_utc_ms=old_start, received_utc_ms=old_start + 40_000)
    old.store_all(pattern_pcm([("t", 5), ("s", 25), ("s", 30), ("s", 10)]), last_cut="shutdown")
    young = RawSegments(cfg, conn, device_id="young", start_utc_ms=RAW_T0 - 2 * DAY,
                        received_utc_ms=RAW_T0 - 2 * DAY + 40_000)
    young.store_all(pattern_pcm([("s", 30), ("s", 10)]), last_cut="shutdown")
    stale = RawSegments(cfg, conn, device_id="stale", start_utc_ms=old_start, received_utc_ms=old_start + 40_000)
    stale.store_all(pattern_pcm([("s", 10)]), last_cut="shutdown")  # never segmented (stays pending)

    make_segmenter(cfg, conn, RAW_T0).run_once()
    conn.execute("UPDATE raw_segments SET status = 'pending' WHERE device_id = 'stale'")
    assert len(derived(conn)) == 1
    files_before = sorted(str(p.relative_to(cfg.archive_dir)) for p in cfg.archive_dir.rglob("*.opus"))
    assert len(files_before) == 6

    out = purge_raw(conn, cfg.archive_dir, now=RAW_T0)
    assert out["deleted"] == 2 and out["bytes"] > 0
    rows = [dict(r) for r in conn.execute("SELECT device_id, n_start, path FROM raw_segments ORDER BY device_id, n_start")]
    assert [(r["device_id"], r["n_start"]) for r in rows] == [
        ("s22", 0),  # overlaps the derived chunk
        ("stale", 0),  # old but still pending
        ("young", 0), ("young", SEGMENT),
    ]
    files_after = sorted(str(p.relative_to(cfg.archive_dir)) for p in cfg.archive_dir.rglob("*.opus"))
    assert files_after == sorted(r["path"] for r in rows)
    assert not list(cfg.archive_dir.rglob("*.json.tmp"))
    for p in cfg.archive_dir.rglob("*.json"):
        assert p.with_suffix(".opus").exists()
    # the derived chunk's audio is still reachable
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0))
    row = conn.execute("SELECT * FROM chunks").fetchone()
    assert len(worker.audio_for(Chunk.from_row(row))) == row["n_samples"]
    # a second purge is a no-op; 29 days is not old enough
    assert purge_raw(conn, cfg.archive_dir, now=RAW_T0)["deleted"] == 0
    assert purge_raw(conn, cfg.archive_dir, now=RAW_T0 + 27 * DAY)["deleted"] == 0
    assert purge_raw(conn, cfg.archive_dir, now=RAW_T0 + 29 * DAY)["deleted"] == 2  # young, now old
    conn.close()


def test_worker_purges_once_a_day(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    clock = {"now": RAW_T0}
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0), now=lambda: clock["now"])
    assert worker.maybe_purge_raw() == {"deleted": 0, "bytes": 0}
    clock["now"] += DAY - 1
    assert worker.maybe_purge_raw() is None
    clock["now"] += 1
    assert worker.maybe_purge_raw() == {"deleted": 0, "bytes": 0}
    conn.close()


def test_raw_audio_span_refuses_gaps(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    plan = fx.plan(pattern_pcm([("s", 30), ("s", 30), ("s", 10)]), last_cut="shutdown")
    fx.store(*plan[0])
    fx.store(*plan[2])
    from roomlog_server.segmenter import covering_segments
    segs = covering_segments(conn, "s22", fx.run_id, 0, 0, 3 * SEGMENT)
    raw = RawAudio(cfg.archive_dir)
    assert len(raw.span(segs, 100, SEGMENT)) == SEGMENT - 100
    with pytest.raises(ValueError):
        raw.span(segs, SEGMENT - 10, 2 * SEGMENT + 10)
    with pytest.raises(ValueError):
        raw.span(segs, 2 * SEGMENT, 3 * SEGMENT)
    conn.close()


def test_purge_raw_rechecks_each_row_before_deleting(tmp_path):
    """A `resegment` that reopens a row between purge's select and delete keeps row and file."""
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    old_start = RAW_T0 - 40 * DAY
    old = RawSegments(cfg, conn, start_utc_ms=old_start, received_utc_ms=old_start + 40_000)
    old.store_all(pattern_pcm([("s", 30), ("s", 30), ("s", 5)]), last_cut="shutdown")
    make_segmenter(cfg, conn, RAW_T0).run_once()
    assert raw_status(conn) == ["segmented"] * 3
    victim = conn.execute("SELECT id, path FROM raw_segments WHERE n_start = ?", (SEGMENT,)).fetchone()

    class Racy:
        """Reopens the middle row right after purge's candidate select."""

        def execute(self, sql, params=()):
            cur = conn.execute(sql, params)
            if sql.lstrip().startswith("SELECT id, path FROM raw_segments"):
                conn.execute("UPDATE raw_segments SET status = 'pending' WHERE id = ?", (victim["id"],))
            return cur

    out = purge_raw(Racy(), cfg.archive_dir, now=RAW_T0)
    assert out["deleted"] == 2
    assert [dict(r) for r in conn.execute("SELECT id, status FROM raw_segments")] == \
        [{"id": victim["id"], "status": "pending"}]
    assert (cfg.archive_dir / victim["path"]).exists()
    assert (cfg.archive_dir / victim["path"]).with_suffix(".json").exists()
    conn.close()
