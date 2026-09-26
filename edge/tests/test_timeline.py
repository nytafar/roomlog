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
    r, n_mic = res[3]
    assert r.epoch == 1
    assert abs(tl.utc_ns(r.n_start) - mic.true_real(n_mic)) < 1_000_000


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
    assert tl.stepped_since(chunk_start)
    assert not tl.stepped_since(r.n_start + 1)
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
