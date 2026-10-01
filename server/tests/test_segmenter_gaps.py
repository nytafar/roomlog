"""Segmenter over gaps, out-of-order arrivals and randomised patterns.

These tests bypass ffmpeg: raw rows point at fake paths and the segmenter's `decode` returns
the stored PCM, so every sample decision is exact and hundreds of runs are cheap.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import numpy as np

from roomlog_server import db as dbmod
from roomlog_server.archive import insert_raw_row, raw_relpath
from roomlog_server.backends import Router
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.segmenter import EnergyVad, Segmenter, covering_segments, reset_segmentation
from roomlog_server.times import ms_to_iso
from roomlog_server.worker import Chunk, Worker

from conftest import RATE, RAW_T0, SEGMENT, make_config, make_raw_sidecar, meta_header, pattern_pcm

W = 512
PAD = 4800
IDLE_MS = 120_000


class FakeRaw:
    """Raw rows whose 'files' are int16 PCM held in memory; `decode` is the segmenter's decoder."""

    def __init__(self, conn, device_id: str = "s22", run_id: str = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b",
                 epoch: int = 0, start_utc_ms: int = RAW_T0) -> None:
        self.conn = conn
        self.device_id = device_id
        self.run_id = run_id
        self.epoch = epoch
        self.start_utc_ms = start_utc_ms
        self.pcm: dict[str, np.ndarray] = {}

    def decode(self, path: str) -> np.ndarray:
        return self.pcm[Path(path).name]

    def plan(self, pcm: np.ndarray, last_cut: str = "shutdown") -> list[tuple[int, np.ndarray, str]]:
        starts = list(range(0, len(pcm), SEGMENT))
        return [(i, pcm[i:i + SEGMENT], last_cut if i == starts[-1] else "cap") for i in starts]

    def store(self, n_start: int, piece: np.ndarray, cut_reason: str, received_utc_ms: int,
              salt: str = "") -> dict:
        body = (piece.astype("<i2").tobytes() + self.device_id.encode() + str(self.epoch).encode()
                + str(n_start).encode() + salt.encode())
        meta = make_raw_sidecar(body, device_id=self.device_id,
                                start_utc=ms_to_iso(self.start_utc_ms + n_start * 1000 // RATE),
                                n_start=n_start, n_samples=len(piece), cut_reason=cut_reason,
                                run_id=self.run_id, epoch=self.epoch)
        rel = raw_relpath(self.start_utc_ms + n_start * 1000 // RATE, hashlib.sha256(body).hexdigest())
        insert_raw_row(self.conn, meta, meta_header(meta), meta["sha256"], rel, received_utc_ms)
        self.pcm[Path(rel).name] = piece.astype(np.float32) / 32768.0
        return meta


def segmenter(fake: FakeRaw, cfg, now_ms: int) -> Segmenter:
    return Segmenter(fake.conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"},
                     decode=fake.decode, now=lambda: now_ms)


def intervals(conn, device_id: str) -> list[tuple[int, int]]:
    rows = conn.execute("SELECT n_start, n_samples FROM chunks WHERE kind = 'derived' AND device_id = ? "
                        "ORDER BY n_start", (device_id,))
    return [(r[0], r[0] + r[1]) for r in rows]


def assert_disjoint(ivs: list[tuple[int, int]]) -> None:
    for (a, b), (c, d) in zip(ivs, ivs[1:]):
        assert b <= c, f"derived chunks overlap: ({a},{b}) and ({c},{d})"
    assert all(b > a for a, b in ivs)


def tone_regions(pattern: list[tuple[str, float]]) -> list[tuple[int, int]]:
    out, n = [], 0
    for kind, seconds in pattern:
        k = int(round(seconds * RATE))
        if kind == "t":
            out.append((n, n + k))
        n += k
    return out


def assert_covers_tone(ivs: list[tuple[int, int]], pattern: list[tuple[str, float]],
                       missing: list[tuple[int, int]] = (), min_tone_s: float = 0.0) -> None:
    """Every tone sample (one window in from its edges) lies in a chunk, unless in `missing`
    or in a tone shorter than `min_tone_s` (the chunker drops speech under 250 ms)."""
    for a, b in tone_regions(pattern):
        if b - a < min_tone_s * RATE:
            continue
        for n in range(a + W, b - W, W):
            if any(lo <= n < hi for lo, hi in missing):
                continue
            assert any(lo <= n < hi for lo, hi in ivs), f"tone sample {n} ({n / RATE:.2f} s) is in no chunk"


def statuses(conn, device_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT status FROM raw_segments WHERE device_id = ? ORDER BY n_start",
                                       (device_id,))]


TALK = [("t", 210)]  # continuous speech: cap cuts every 30 s
CYCLES = [("s", 2), ("t", 6), ("s", 2)] * 21  # 10 s cycles, seams in silence


def pcm_for(pattern: list[tuple[str, float]], amp: int = 8000, phase: float = 0.0) -> np.ndarray:
    """`pattern_pcm` with a slow amplitude drift, so no two 30 s stretches of tone are
    sample-identical (identical PCM would hash to one sha and hide a chunk); `phase` makes
    two patterns that both open with a long tone differ too."""
    pcm = pattern_pcm(pattern, amp=amp).astype(np.float64)
    t = np.arange(len(pcm)) / RATE
    return (pcm * (0.8 + 0.2 * np.sin(2 * np.pi * (t / 1000.0 + phase)))).astype(np.int16)


# ---------------------------------------------------------------- gaps inside an epoch


def _gap_case(tmp_path, pattern, arrival: list[int], step_ms: int, last_now_ms: int | None = None):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fake = FakeRaw(conn)
    plan = fake.plan(pcm_for(pattern), last_cut="shutdown")
    t = RAW_T0 + 220_000
    for i, idx in enumerate(arrival):
        n, piece, reason = plan[idx]
        received = t + i * step_ms
        fake.store(n, piece, reason, received)
        segmenter(fake, cfg, received + 1000).run_once()
        assert_disjoint(intervals(conn, "s22"))
    if last_now_ms is not None:
        segmenter(fake, cfg, last_now_ms).run_once()
    return cfg, conn, fake, plan


def test_segment_missing_for_good_closes_the_earlier_chain_at_the_gap(tmp_path):
    """Reviewer's reproduction 1: seven segments, the fourth never arrives."""
    for pattern in (TALK, CYCLES):
        cfg, conn, fake, plan = _gap_case(tmp_path / pattern[0][0] / str(len(pattern)), pattern,
                                          [0, 1, 2, 4, 5, 6], step_ms=5000)
        # the gap is younger than raw_idle_s: the first chain still waits, so does the second
        assert statuses(conn, "s22")[2:] == ["pending"] * 4
        segmenter(fake, cfg, RAW_T0 + 220_000 + 5 * 5000 + IDLE_MS + 1).run_once()
        ivs = intervals(conn, "s22")
        assert_disjoint(ivs)
        assert_covers_tone(ivs, pattern, missing=[(3 * SEGMENT, 4 * SEGMENT)])
        assert not any(lo < 4 * SEGMENT and hi > 3 * SEGMENT for lo, hi in ivs)  # nothing derived from the gap
        assert statuses(conn, "s22") == ["segmented"] * 6
        # and a further pass is a no-op
        res = segmenter(fake, cfg, RAW_T0 + 400_000).run_once()
        assert (res.epochs, res.chunks) == (0, 0)
        conn.close()


def test_out_of_order_arrival_fills_the_gap_without_overlap(tmp_path):
    """Reviewer's reproduction 2: arrival order 0,1,2,4,5,6,3 with a pass after each."""
    for pattern in (TALK, CYCLES):
        for step_ms in (5000, IDLE_MS + 1000):  # the gap fills while young, or after it aged
            cfg, conn, fake, plan = _gap_case(tmp_path / pattern[0][0] / str(len(pattern)) / str(step_ms),
                                              pattern, [0, 1, 2, 4, 5, 6, 3], step_ms=step_ms)
            ivs = intervals(conn, "s22")
            assert_disjoint(ivs)
            assert_covers_tone(ivs, pattern)
            assert statuses(conn, "s22") == ["segmented"] * 7
            res = segmenter(fake, cfg, RAW_T0 + 900_000).run_once()
            assert (res.epochs, res.chunks) == (0, 0)
            assert intervals(conn, "s22") == ivs
            conn.close()


def test_young_gap_fills_and_matches_one_shot(tmp_path):
    """A segment one upload late (409 retry) must not change the chunk set at all."""
    # Two databases: the same PCM in one would hash the second device's chunks to existing shas.
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    pcm = pcm_for(CYCLES)
    one = FakeRaw(conn, device_id="one")
    for n, piece, reason in one.plan(pcm):
        one.store(n, piece, reason, RAW_T0 + 220_000)
    segmenter(one, cfg, RAW_T0 + 221_000).run_once()
    cfg2 = make_config(tmp_path / "late")
    conn2 = dbmod.connect(cfg2.db_path)
    late = FakeRaw(conn2, device_id="one")
    plan = late.plan(pcm)
    t = RAW_T0 + 220_000
    for i, idx in enumerate([0, 1, 2, 4, 3, 5, 6]):
        late.store(*plan[idx], t + i * 1000)
        segmenter(late, cfg2, t + i * 1000 + 500).run_once()
    assert intervals(conn2, "one") == intervals(conn, "one")
    conn.close()
    conn2.close()


# ---------------------------------------------------------------- randomised: incremental == one shot


def random_pattern(rng: random.Random) -> list[tuple[str, float]]:
    tones = [0.1, 0.2, 0.3, 0.4, 0.6, 1.0, 2.0, 4.0, 8.0, 16.0, 35.0]
    silences = [0.1, 0.3, 0.6, 1.0, 1.4, 1.6, 2.0, 3.0, 6.0, 12.0]
    total_s = rng.uniform(75, 160)
    out: list[tuple[str, float]] = []
    kind = rng.choice("st")
    n = 0.0
    while n < total_s:
        d = rng.choice(tones if kind == "t" else silences) * rng.uniform(0.7, 1.3)
        out.append((kind, round(d, 3)))
        n += d
        kind = "s" if kind == "t" else "t"
    return out


def test_incremental_passes_equal_one_shot_on_random_patterns(tmp_path):
    # One database per arrival mode: the same PCM in one database would hash the incremental
    # device's chunks to the one-shot shas and hide them.
    cfg = make_config(tmp_path / "one")
    conn = dbmod.connect(cfg.db_path)
    cfg2 = make_config(tmp_path / "inc")
    conn2 = dbmod.connect(cfg2.db_path)
    runs = 150
    for seed in range(runs):
        rng = random.Random(seed)
        pattern = random_pattern(rng)
        pcm = pcm_for(pattern, amp=rng.choice([4000, 8000, 12000]), phase=rng.random())
        last_cut = rng.choice(["shutdown", "discontinuity"])
        one = FakeRaw(conn, device_id=f"d{seed}")
        for n, piece, reason in one.plan(pcm, last_cut):
            one.store(n, piece, reason, RAW_T0)
        segmenter(one, cfg, RAW_T0 + 1000).run_once()
        expected = intervals(conn, one.device_id)

        inc = FakeRaw(conn2, device_id=f"d{seed}")
        for i, (n, piece, reason) in enumerate(inc.plan(pcm, last_cut)):
            inc.store(n, piece, reason, RAW_T0 + i * 1000)
            segmenter(inc, cfg2, RAW_T0 + i * 1000 + 500).run_once()
        got = intervals(conn2, inc.device_id)
        assert got == expected, f"seed {seed}: incremental {got} != one-shot {expected}"
        assert_disjoint(got)
        assert_covers_tone(got, pattern, min_tone_s=0.3)
        assert statuses(conn2, inc.device_id) == ["segmented"] * len(inc.plan(pcm))
    conn.close()
    conn2.close()


# ---------------------------------------------------------------- duplicate raw rows (review 2)


def test_duplicate_raw_rows_play_once_in_segmenter_and_worker(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fake = FakeRaw(conn)
    plan = fake.plan(pcm_for([("s", 5), ("t", 40), ("s", 15)]))
    for n, piece, reason in plan:
        fake.store(n, piece, reason, RAW_T0)
    fake.store(*plan[0], RAW_T0 + 10, salt="re-upload")  # the same span again, another sha
    fake.store(*plan[1], RAW_T0 + 10, salt="re-upload")
    res = segmenter(fake, cfg, RAW_T0 + 1000).run_once()
    assert res.errors == []
    ivs = intervals(conn, "s22")
    assert_disjoint(ivs)
    assert_covers_tone(ivs, [("s", 5), ("t", 40), ("s", 15)])
    assert statuses(conn, "s22") == ["segmented"] * 4
    # the chunk across the seam decodes to exactly its n_samples through both duplicated rows
    segs = covering_segments(conn, "s22", fake.run_id, 0, 0, 2 * SEGMENT)
    assert [s.n_start for s in segs] == [0, SEGMENT]  # one row per span
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0), decode=fake.decode)
    for row in conn.execute("SELECT * FROM chunks WHERE kind = 'derived'"):
        assert len(worker.audio_for(Chunk.from_row(row))) == row["n_samples"]
    conn.close()


# ---------------------------------------------------------------- resegment a range (review 3)


def test_resegment_range_in_the_middle_of_a_run_touches_only_that_extent(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fake = FakeRaw(conn)
    pattern = [("s", 5)] + [("t", 6), ("s", 4)] * 29 + [("s", 5)]  # 300 s; tones 5-11, 15-21, ... 285-291
    for n, piece, reason in fake.plan(pcm_for(pattern)):
        fake.store(n, piece, reason, RAW_T0 + 300_000)
    segmenter(fake, cfg, RAW_T0 + 301_000).run_once()
    before = [dict(r) for r in conn.execute("SELECT id, n_start, n_samples, sha256 FROM chunks ORDER BY n_start")]
    assert len(before) == 29 and statuses(conn, "s22") == ["segmented"] * 10
    conn.execute("UPDATE chunks SET status = 'done'")
    for c in before:
        conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) "
                     "VALUES (?, 's22', 0, 0, 1, 0, 'x', 'm')", (c["id"],))

    # segments starting in [100 s, 130 s): only the one at 120 s. Its tones: 125-131 (straddles the
    # seam at 150 s? no: 145-151 does), and the 115-121 tone straddles 120 s.
    out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + 100_000, to_ms=RAW_T0 + 130_000)
    hit = [c for c in before if c["n_start"] < 5 * SEGMENT and c["n_start"] + c["n_samples"] > 4 * SEGMENT]
    assert len(hit) == 4  # 115-121, 125-131, 135-141, 145-151
    assert out == {"epochs": 1, "raw_reset": 3, "chunks_deleted": 4}  # raw 3, 4, 5 (the straddlers reach into 3 and 5)
    assert statuses(conn, "s22") == ["segmented"] * 3 + ["pending"] * 3 + ["segmented"] * 4
    left = [dict(r) for r in conn.execute("SELECT id, n_start, n_samples, sha256 FROM chunks ORDER BY n_start")]
    assert left == [c for c in before if c not in hit]  # the others keep their ids and transcripts
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 25
    assert conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] == 10 * SEGMENT  # the mark stays

    res = segmenter(fake, cfg, RAW_T0 + 400_000).run_once()
    assert res.errors == [] and res.chunks == 4
    after = [dict(r) for r in conn.execute("SELECT n_start, n_samples, sha256 FROM chunks ORDER BY n_start")]
    assert after == [{k: c[k] for k in ("n_start", "n_samples", "sha256")} for c in before]
    assert statuses(conn, "s22") == ["segmented"] * 10
    assert_disjoint(intervals(conn, "s22"))
    # the untouched chunks are still done; the four re-derived ones are pending
    assert conn.execute("SELECT count(*) FROM chunks WHERE status = 'pending'").fetchone()[0] == 4

    # a range that reaches the epoch's end moves the mark back and re-derives the tail in order
    out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + 240_000)
    # segments 8 and 9 selected; the 235-241 tone straddles 240 s, so segment 7 reopens too
    assert out["raw_reset"] == 3 and out["chunks_deleted"] == 6  # 235-241 ... 285-291
    assert conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] < 8 * SEGMENT
    segmenter(fake, cfg, RAW_T0 + 500_000).run_once()
    assert [dict(r) for r in conn.execute("SELECT n_start, n_samples, sha256 FROM chunks ORDER BY n_start")] == after
    conn.close()


# ---------------------------------------------------------------- budget and heartbeat (review 7)


def test_run_once_budget_stops_between_epochs_and_heartbeats_per_pass(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    beats = []
    fakes = []
    for epoch in range(3):
        f = FakeRaw(conn, epoch=epoch, start_utc_ms=RAW_T0 + epoch * 100_000)
        for n, piece, reason in f.plan(pcm_for([("s", 5), ("t", 10), ("s", 20)], amp=4000 + 1000 * epoch)):
            f.store(n, piece, reason, RAW_T0)
        fakes.append(f)
    decode = {}
    for f in fakes:
        decode.update(f.pcm)
    seg = Segmenter(conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"},
                    decode=lambda path: decode[Path(path).name], now=lambda: RAW_T0 + 1000,
                    heartbeat=lambda: beats.append(1))
    res = seg.run_once(budget_s=0.0)
    assert (res.epochs, res.chunks, res.more) == (1, 1, True)
    assert len(beats) >= 1
    res = seg.run_once()
    assert (res.epochs, res.chunks, res.more) == (2, 2, False)
    assert seg.run_once(budget_s=0.0).more is False  # nothing pending: no work, no flag
    conn.close()


# ---------------------------------------------------------------- decoded length (review 11)


def test_short_decode_fails_the_segment_and_the_neighbours_go_on(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fake = FakeRaw(conn)
    plan = fake.plan(pcm_for(CYCLES))  # 7 segments
    for n, piece, reason in plan:
        fake.store(n, piece, reason, RAW_T0 + 220_000)
    names = [Path(m["path"]).name for m in
             [dict(r) for r in conn.execute("SELECT path FROM raw_segments ORDER BY n_start")]]
    good = dict(fake.pcm)
    fake.pcm[names[1]] = good[names[1]][:-40]  # within tolerance: fitted quietly
    fake.pcm[names[3]] = good[names[3]][:-2000]  # a broken file

    res = segmenter(fake, cfg, RAW_T0 + 221_000).run_once()
    assert res.raw_failed == 1 and res.more and len(res.errors) == 1 and "sidecar says 480000" in res.errors[0]
    assert statuses(conn, "s22")[3] == "failed"
    # the failed one is a gap: once it is old enough both sides are closed around it
    segmenter(fake, cfg, RAW_T0 + 221_000 + IDLE_MS).run_once()
    ivs = intervals(conn, "s22")
    assert_disjoint(ivs)
    assert_covers_tone(ivs, CYCLES, missing=[(3 * SEGMENT, 4 * SEGMENT)])
    st = statuses(conn, "s22")
    assert st[3] == "failed" and [x for i, x in enumerate(st) if i != 3] == ["segmented"] * 6
    assert conn.execute("SELECT count(*) FROM raw_segments WHERE status = 'pending'").fetchone()[0] == 0
    # nothing left to do: the failed row is not retried every pass
    res = segmenter(fake, cfg, RAW_T0 + 400_000).run_once()
    assert (res.epochs, res.errors) == (0, [])
    # a resegment over its time gives it another chance; with the file fixed it fills the gap
    fake.pcm[names[3]] = good[names[3]]
    out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + 90_000, to_ms=RAW_T0 + 91_000)
    assert out["raw_reset"] == 1 and statuses(conn, "s22")[3] == "pending"
    res = segmenter(fake, cfg, RAW_T0 + 401_000).run_once()
    assert res.errors == [] and res.chunks == 3
    assert_covers_tone(intervals(conn, "s22"), CYCLES)
    conn.close()


# ---------------------------------------------------------------- follow-up review A and B


def test_resegment_on_a_live_epoch_rederives_the_whole_tail_without_overlap(tmp_path):
    """A bounded range one segment before the mark's segment, while the last segment still
    waits for its successor: the tail from the range on is redone whole, never twice."""
    pattern = [("s", 1.2), ("t", 2.5), ("s", 0.9), ("t", 4), ("s", 1.4)] * 24  # 240 s, pauses under 1.5 s
    for seed in range(12):
        cfg = make_config(tmp_path / str(seed))
        conn = dbmod.connect(cfg.db_path)
        fake = FakeRaw(conn)
        plan = fake.plan(pcm_for(pattern, phase=seed / 12), last_cut="cap")  # live: no final cut
        kw = {"chunker_kwargs": {"min_silence_ms": 700}}
        for i, (n, piece, reason) in enumerate(plan):
            fake.store(n, piece, reason, RAW_T0 + i * 1000)
            Segmenter(conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"}, decode=fake.decode,
                      now=lambda i=i: RAW_T0 + i * 1000 + 500, **kw).run_once()
        before = intervals(conn, "s22")
        mark = conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0]
        assert mark < 7 * SEGMENT and statuses(conn, "s22")[-1] == "pending"
        k = mark // SEGMENT - 1  # the segment one step before the mark's segment
        out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + k * 30_000, to_ms=RAW_T0 + (k + 1) * 30_000)
        left = intervals(conn, "s22")
        assert out["chunks_deleted"] == len(before) - len(left)
        assert not any(hi > conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] for _, hi in left)
        res = Segmenter(conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"}, decode=fake.decode,
                        now=lambda: RAW_T0 + 60_000, **kw).run_once()
        assert res.errors == []
        after = intervals(conn, "s22")
        assert_disjoint(after)
        assert after == before, f"seed {seed}"
        assert conn.execute("SELECT segmented_to_n FROM raw_progress").fetchone()[0] == mark
        conn.close()


def test_failed_upload_is_not_played_for_a_chunk_from_its_good_re_upload(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fake = FakeRaw(conn)
    pattern = [("s", 5), ("t", 40), ("s", 15)]
    plan = fake.plan(pcm_for(pattern))
    for n, piece, reason in plan:
        fake.store(n, piece, reason, RAW_T0)
    broken = [Path(r[0]).name for r in conn.execute("SELECT path FROM raw_segments ORDER BY n_start")][1]
    fake.pcm[broken] = fake.pcm[broken][:-5000]
    res = segmenter(fake, cfg, RAW_T0 + 1000).run_once()
    assert res.raw_failed == 1 and statuses(conn, "s22")[1] == "failed"
    fake.store(*plan[1], RAW_T0 + 5000, salt="re-upload")  # the good file, a higher id
    res = segmenter(fake, cfg, RAW_T0 + 6000).run_once()
    assert res.errors == [] and res.chunks == 2  # the 40 s tone is cap-cut once
    ivs = intervals(conn, "s22")
    assert_disjoint(ivs)
    assert_covers_tone(ivs, pattern)
    segs = covering_segments(conn, "s22", fake.run_id, 0, 0, 2 * SEGMENT)  # 60 s: two spans, three rows
    assert [(s.n_start, s.status) for s in segs] == [(0, "segmented"), (SEGMENT, "segmented")]
    assert Path(segs[1].path).name != broken
    worker = Worker(cfg, conn, Router([FakeBackend()], probe_ttl_s=0), decode=fake.decode)
    for row in conn.execute("SELECT * FROM chunks"):
        assert len(worker.audio_for(Chunk.from_row(row))) == row["n_samples"]
    from roomlog_server.verify import verify_archive
    report = verify_archive(conn, cfg.archive_dir)
    assert report["derived"] == 2 and not [p for p in report["problems"] if "raw audio missing" in p]
    conn.close()
