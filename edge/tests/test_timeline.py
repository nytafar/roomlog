"""Timeline tests (design §3.3, §5): synthetic block records, no clocks."""

import random

from roomlog_edge.timeline import NS, Block, Timeline

RATE = 16000
W = 512
BLOCK_NS = W * NS // RATE  # 32 ms
LAT = 40_000_000  # 40 ms PortAudio latency
MONO0 = 1_000 * NS
REAL0 = 1_758_880_000 * NS


class Mic:
    """Produces blocks the way a PortAudio callback would report them.

    ``true_ns(n)`` is the wall-clock time the mic actually sampled ``n``; the
    test compares the timeline's mapping against it.
    """

    def __init__(self, ppm=0.0, jitter_ns=0, seed=1):
        self.n_mic = 0  # samples the mic has produced (including lost ones)
        self.ratio = 1.0 + ppm * 1e-6  # seconds per nominal second
        self.jitter = jitter_ns
        self.rng = random.Random(seed)
        self.offset = REAL0 - MONO0
        self.extra_delay = 0

    def true_mono(self, n_mic):
        return MONO0 + round(n_mic * NS * self.ratio / RATE)

    def true_real(self, n_mic):
        return self.true_mono(n_mic) + self.offset

    def block(self, overflow=False, delay_ns=0, honest=False):
        """One 512-frame callback. ``delay_ns`` delays the callback; when
        ``honest`` PortAudio's latency figure grows with it (ALSA timestamps
        come from the hardware), otherwise the delay shows up as lateness."""
        n0 = self.n_mic
        self.n_mic += W
        adc_end = self.true_mono(self.n_mic)
        jitter = self.rng.randrange(self.jitter + 1) if self.jitter else 0
        m = adc_end + LAT + jitter + delay_ns
        lat = (adc_end - self.true_mono(n0)) + LAT + (delay_ns if honest else 0)
        return Block(W, m, m + self.offset, lat, overflow), n0


def run(tl, mic, blocks, **kw):
    out = []
    for _ in range(blocks):
        b, n_mic = mic.block(**kw)
        out.append((tl.feed(b), n_mic))
    return out


def test_steady_state_within_1ms():
    tl, mic = Timeline(), Mic()
    res = run(tl, mic, 2000)
    assert res[0][0].new_epoch and res[0][0].epoch == 0
    for r, n_mic in res[1:]:
        assert r.epoch == 0 and not r.new_epoch and r.clock_step_ns is None
        assert abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)) < 1_000_000
    assert tl.n == 2000 * W


def test_input_overflow_opens_new_epoch():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 100)
    mic.n_mic += 5 * W  # the hardware dropped five blocks
    (r, n_mic), = run(tl, mic, 1, overflow=True)
    assert r.new_epoch and r.epoch == 1
    assert r.n_start == 100 * W  # n keeps counting received samples
    assert abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)) < 1_000_000
    for r, n_mic in run(tl, mic, 50):
        assert not r.new_epoch and r.epoch == 1
        assert abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)) < 1_000_000


def test_silent_loss_detected_within_three_blocks():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 100)
    mic.n_mic += 7 * W  # 224 ms lost, no flag
    res = run(tl, mic, 5)
    flags = [r.new_epoch for r, _ in res]
    assert flags == [False, False, True, False, False]
    assert [r.lateness_ns > 200_000_000 for r, _ in res[:3]] == [True] * 3
    # The epoch starts at the first late block, i.e. the first post-gap
    # sample, so the two blocks seen before the decision belong to it too.
    r2, _ = res[2]
    assert r2.epoch_start_n == 100 * W and r2.n_start == 102 * W
    assert tl.epochs[0].end_n == 100 * W
    for r, n_mic in res[2:]:
        assert r.epoch == 1
        assert abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)) < 1_000_000
    for r, n_mic in res[:2]:  # the first two late blocks map through the new epoch
        assert abs(tl.utc_ns(r.n_start, 1) - mic.true_real(n_mic)) < 1_000_000


def test_scheduling_hiccup_with_catchup_keeps_epoch():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 100)
    # 150 ms late callback: PortAudio's latency figure grows with the delay,
    # so t_b is still right; then queued blocks arrive back to back.
    b, _ = mic.block(delay_ns=150_000_000, honest=True)
    r = tl.feed(b)
    assert not r.new_epoch and abs(r.lateness_ns) < 1_000_000
    # Cruder hiccup where the latency figure did not grow: two late blocks
    # then catch-up must still not trip the three-block rule.
    for _ in range(2):
        b, _ = mic.block(delay_ns=250_000_000)
        r = tl.feed(b)
        assert not r.new_epoch and r.lateness_ns > 200_000_000
    for r, _ in run(tl, mic, 20):
        assert not r.new_epoch and r.epoch == 0


def _clock_step_case(delta_ns):
    tl, mic = Timeline(), Mic()
    run(tl, mic, 200)
    chunk_start = 150 * W
    before = tl.utc_ns(chunk_start)
    mic.offset += delta_ns  # the wall clock steps; monotonic does not
    (r, n_mic), = run(tl, mic, 1)
    assert r.clock_step_ns == delta_ns and not r.new_epoch and r.epoch == 0
    assert tl.stepped_in(chunk_start, r.n_start + W)
    assert not tl.stepped_in(chunk_start, r.n_start)  # chunk ended before the step
    assert not tl.stepped_in(r.n_start + 1, r.n_start + 10 * W)
    assert tl.step_count == 1
    # In-flight chunk re-stamped with the post-step mapping.
    assert tl.utc_ns(chunk_start) == before + delta_ns
    # Later chunks: no gap, sample-exact relative timing, same epoch.
    res = run(tl, mic, 300)
    for r2, n2 in res:
        assert r2.epoch == 0 and not r2.new_epoch and r2.clock_step_ns is None
        assert abs(tl.utc_ns(r2.n_start) - mic.true_real(n2)) < 1_000_000
    a, b = res[10][0], res[11][0]
    assert tl.utc_ns(b.n_start) - tl.utc_ns(a.n_start) == BLOCK_NS


def test_clock_step_forward_one_hour():
    _clock_step_case(3600 * NS)


def test_clock_step_backward_two_seconds():
    _clock_step_case(-2 * NS)


def test_drift_50ppm_one_hour_under_10ms():
    for ppm in (50.0, -50.0):
        tl, mic = Timeline(), Mic(ppm=ppm, jitter_ns=5_000_000)
        blocks = 3600 * RATE // W
        worst_now = worst_later = 0
        res = []
        for _ in range(blocks):
            b, n_mic = mic.block()
            r = tl.feed(b)
            assert r.epoch == 0
            res.append((r, n_mic))
            # stamped when the chunk closes, i.e. right away
            worst_now = max(worst_now, abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)))
        for r, n_mic in res:
            # stamped an hour later (unsynced hold release)
            worst_later = max(worst_later, abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)))
        assert worst_now < 10_000_000, (ppm, worst_now)
        assert worst_later < 10_000_000, (ppm, worst_later)


def test_unsynced_start_then_sync_restamps_held_chunks():
    """The clock is 90 s slow at boot; sync steps it forward. Chunks stamped
    before the step are recomputed through the same epoch mapping."""
    tl, mic = Timeline(), Mic()
    true_offset = mic.offset
    mic.offset -= 90 * NS
    run(tl, mic, 300)
    held = [(0, 20 * W), (0, 120 * W)]  # (epoch, n_start) of chunks in unsynced/
    stale = [tl.utc_ns(n, e) for e, n in held]
    mic.offset = true_offset
    (r, _), = run(tl, mic, 1)
    assert r.clock_step_ns == 90 * NS
    for (e, n), old in zip(held, stale):
        assert tl.utc_ns(n, e) == old + 90 * NS
        assert tl.utc_ns(n, e) == mic.true_real(n)


def test_restamp_across_epochs_after_step():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 50)
    mic.n_mic += 3 * W
    run(tl, mic, 1, overflow=True)
    run(tl, mic, 50)
    old0, old1 = tl.utc_ns(10 * W, 0), tl.utc_ns(60 * W, 1)
    mic.offset += 7 * NS
    run(tl, mic, 1)
    assert tl.utc_ns(10 * W, 0) == old0 + 7 * NS
    assert tl.utc_ns(60 * W, 1) == old1 + 7 * NS


def test_step_seen_on_epoch_opening_block_shifts_old_epochs():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 100)
    old = tl.utc_ns(50 * W, 0)
    mic.offset += 5 * NS
    mic.n_mic += 4 * W
    (r, n_mic), = run(tl, mic, 1, overflow=True)
    assert r.new_epoch and r.epoch == 1 and r.clock_step_ns == 5 * NS
    assert tl.last_step_n == r.n_start and tl.step_count == 1
    assert tl.utc_ns(50 * W, 0) == old + 5 * NS
    assert abs(tl.utc_ns(r.n_start, 1) - mic.true_real(n_mic)) < 1_000_000


def test_observe_offset_applies_step_outside_callbacks():
    tl, mic = Timeline(), Mic()
    run(tl, mic, 100)
    old = tl.utc_ns(10 * W)
    assert tl.observe_offset(mic.true_real(0) + 1_000_000, mic.true_mono(0)) is None  # 1 ms: no step
    delta = tl.observe_offset(mic.true_real(0) + 90 * NS, mic.true_mono(0))
    assert delta == 90 * NS and tl.step_count == 1 and tl.last_step_n == tl.n
    assert tl.utc_ns(10 * W) == old + 90 * NS
    # the next block agrees with the new offset: no second step
    mic.offset += 90 * NS
    (r, _), = run(tl, mic, 1)
    assert r.clock_step_ns is None


def test_observed_step_is_not_reversed_by_queued_pre_step_callbacks():
    tl, mic = Timeline(), Mic()
    mic.offset -= 90 * NS
    run(tl, mic, 100)
    queued = [mic.block()[0] for _ in range(2)]
    mic.offset += 90 * NS
    observed_mono = queued[-1].mono_ns + 1_000_000
    assert tl.observe_offset(observed_mono + mic.offset, observed_mono) == 90 * NS
    for block in queued:
        assert tl.feed(block).clock_step_ns is None
    assert tl.step_count == 1
    block, n_mic = mic.block()
    assert tl.feed(block).clock_step_ns is None
    assert tl.step_count == 1
    assert abs(tl.utc_ns(tl.n - W) - mic.true_real(n_mic)) < 1_000_000


def test_anchor_lookup_and_pruning():
    tl, mic = Timeline(), Mic(ppm=50.0)
    blocks = 20 * 60 * RATE // W  # twenty minutes → ~20 anchors
    tl.retain_from_n = 0
    for r, n_mic in run(tl, mic, blocks):
        pass
    ep = tl.epochs[0]
    assert len(ep.anchors) >= 15
    # bisect lookup matches a linear scan for samples all over the range
    for n in range(0, tl.n, 7919 * 5):
        linear = max((a for a in ep.anchors if a[0] <= n), default=ep.anchors[0])
        assert ep.anchor_for(n) == linear
    assert ep.anchor_for(-5) == ep.anchors[0]
    # pruning keeps the anchor that still covers retain_from_n and everything after
    keep_n = ep.anchors[10][0] + 100
    tl.retain_from_n = keep_n
    run(tl, mic, 60 * RATE // W + 5)  # one more window → prune runs
    assert ep.anchors[0][0] <= keep_n < ep.anchors[1][0]
    assert len(ep.anchors) < 15
    # epochs entirely before retain_from_n go too, the current one never does
    mic.n_mic += W
    run(tl, mic, 1, overflow=True)
    tl.retain_from_n = tl.n
    run(tl, mic, 60 * RATE // W + 5)
    assert list(tl.epochs) == [1]
