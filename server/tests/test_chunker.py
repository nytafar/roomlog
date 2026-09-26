"""Chunker tests (design §3.4, §5): synthetic probability sequences."""

from roomlog_server.chunker import Chunker

W = 512
PAD = 4800
S, Q, M = 0.9, 0.1, 0.42  # speech, quiet, in-between (hysteresis zone)


def windows(ms: int) -> int:
    """Number of 32 ms windows covering at least ``ms``."""
    return -(-ms * 16 // W)


def drive(pattern, chunker=None, start=0):
    """``pattern`` is a list of (prob, n_windows). Returns chunks, the chunker
    and the next window index."""
    ch = chunker or Chunker()
    out = []
    i = start
    for p, n in pattern:
        for _ in range(n):
            out.extend(ch.feed(i, p))
            i += 1
    return out, ch, i


def assert_no_overlap(chunks):
    for a, b in zip(chunks, chunks[1:]):
        assert b.n_start >= a.n_end, (a, b)
    for c in chunks:
        assert 0 < c.n_samples <= 480_000


def test_padding_both_sides_and_silence_cut():
    lead = 100
    chunks, _, _ = drive([(Q, lead), (S, windows(2000)), (Q, windows(1600))])
    assert len(chunks) == 1
    c = chunks[0]
    speech_start = lead * W
    speech_end = (lead + windows(2000)) * W
    assert c.n_start == speech_start - PAD
    assert c.n_end == speech_end + PAD
    assert c.cut_reason == "silence" and not c.discontinuity


def test_no_padding_before_origin():
    chunks, _, _ = drive([(S, windows(1000)), (Q, windows(1600))])
    assert chunks[0].n_start == 0


def test_silence_below_1500ms_merges():
    chunks, _, _ = drive([
        (S, windows(1000)), (Q, windows(1400)), (S, windows(1000)), (Q, windows(1600)),
    ])
    assert len(chunks) == 1
    assert chunks[0].n_end == (windows(1000) * 2 + windows(1400)) * W + PAD


def test_silence_of_1500ms_splits():
    a = windows(1000)
    b = 47  # 47 * 32 ms = 1504 ms >= 1500 ms
    chunks, _, _ = drive([(S, a), (Q, b), (S, a), (Q, windows(1600))])
    assert len(chunks) == 2
    assert chunks[0].n_end == a * W + PAD
    assert chunks[1].n_start == (a + b) * W - PAD
    assert_no_overlap(chunks)
    # 46 windows = 1472 ms merges
    chunks, _, _ = drive([(S, a), (Q, 46), (S, a), (Q, windows(1600))])
    assert len(chunks) == 1


def test_hysteresis_zone_neither_starts_nor_ends():
    a = windows(1000)
    chunks, ch, _ = drive([(M, 50), (S, a), (M, windows(3000)), (S, a), (Q, windows(1600))])
    assert len(chunks) == 1
    assert chunks[0].n_start == 50 * W - PAD


def test_speech_shorter_than_250ms_dropped():
    chunks, _, _ = drive([(S, 7), (Q, windows(1600))])  # 224 ms
    assert chunks == []
    chunks, _, _ = drive([(S, 8), (Q, windows(1600))])  # 256 ms
    assert len(chunks) == 1


def test_cap_cuts_at_longest_pause():
    # 12 s speech, 200 ms pause, 10 s speech, 400 ms pause, then speech until the cap.
    pattern = [(S, windows(12000)), (Q, windows(200)), (S, windows(10000)),
               (Q, windows(400)), (S, windows(12000)), (Q, windows(1600))]
    chunks, _, _ = drive(pattern)
    assert len(chunks) == 2
    first, second = chunks
    pause_start = (windows(12000) + windows(200) + windows(10000)) * W
    pause_len = windows(400) * W
    assert first.cut_reason == "cap"
    assert first.n_end == pause_start + pause_len // 2
    assert second.n_start == first.n_end  # nothing lost
    assert second.cut_reason == "silence"
    assert_no_overlap(chunks)


def test_cap_ignores_pauses_shorter_than_100ms():
    pattern = [(S, windows(15000)), (Q, 2), (S, windows(20000)), (Q, windows(1600))]  # 64 ms pause
    chunks, _, _ = drive(pattern)
    assert chunks[0].cut_reason == "cap"
    assert chunks[0].n_samples == 480_000


def test_hard_cap_at_30s_and_continuation():
    chunks, _, _ = drive([(S, windows(70000)), (Q, windows(1600))])
    assert [c.cut_reason for c in chunks] == ["cap", "cap", "silence"]
    assert chunks[0].n_start == 0 and chunks[0].n_samples == 480_000
    assert chunks[1].n_start == 480_000 and chunks[1].n_samples == 480_000
    assert chunks[2].n_start == 960_000
    assert_no_overlap(chunks)


def test_cap_cut_in_running_pause_then_long_silence_emits_no_silence_only_chunk():
    chunks, _, _ = drive([(S, windows(29800)), (Q, windows(3000))])
    assert len(chunks) == 1
    assert chunks[0].cut_reason == "cap"


def test_discontinuity_cut_and_flag():
    chunks, ch, i = drive([(S, windows(3000))])
    cut = ch.cut("discontinuity")
    assert len(cut) == 1 and cut[0].cut_reason == "discontinuity"
    assert cut[0].n_end == i * W
    assert not cut[0].discontinuity
    origin = i * W + 12345
    ch.reset(origin=origin)  # window indices restart at the new origin
    chunks, _, j = drive([(Q, 3), (S, windows(1000)), (Q, windows(1600))], ch)
    assert len(chunks) == 1
    assert chunks[0].discontinuity
    assert chunks[0].n_start == origin  # 96 ms of lead-in is less than the pad
    assert chunks[0].cut_reason == "silence"
    # the flag is consumed
    chunks, _, _ = drive([(S, windows(1000)), (Q, windows(1600))], ch, start=j)
    assert not chunks[0].discontinuity


def test_discontinuity_with_no_open_chunk_still_flags_next():
    _, ch, i = drive([(Q, 10)])
    assert ch.cut("discontinuity") == []
    ch.reset(i * W)
    chunks, _, _ = drive([(S, windows(1000)), (Q, windows(1600))], ch)
    assert chunks[0].n_start == i * W  # no padding before the origin
    assert chunks[0].discontinuity


def test_shutdown_cut():
    _, ch, i = drive([(S, windows(2000)), (Q, 5)])
    cut = ch.cut("shutdown")
    assert len(cut) == 1 and cut[0].cut_reason == "shutdown"
    assert cut[0].n_end == windows(2000) * W + 5 * W  # 160 ms silence < pad
    _, ch, i = drive([(S, windows(2000)), (Q, 20)])
    cut = ch.cut("shutdown")
    assert cut[0].n_end == windows(2000) * W + PAD
    _, ch, _ = drive([(S, 3)])
    assert ch.cut("shutdown") == []  # too short


def test_consecutive_chunks_never_overlap_random():
    import random

    rng = random.Random(7)
    ch = Chunker()
    chunks = []
    i = total = 0
    while total < 200_000:
        p = rng.choice([S, S, S, Q, Q, M])
        n = rng.randrange(1, 400)
        for _ in range(n):
            chunks.extend(ch.feed(i, p))
            i += 1
            total += 1
        if rng.random() < 0.01:
            chunks.extend(ch.cut("discontinuity"))
            ch.reset(ch.origin + i * W + rng.randrange(5000))
            i = 0
    chunks.extend(ch.cut("shutdown"))
    assert len(chunks) > 50
    assert_no_overlap(chunks)


def speech_windows_covered(pattern, chunks, origin=0):
    """Every window at or above threshold lies inside the union of chunks."""
    covered = sorted((c.n_start, c.n_end) for c in chunks)
    merged = []
    for a, b in covered:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    i = 0
    missing = []
    for p, n in pattern:
        for _ in range(n):
            cur = origin + i * W
            if p >= 0.5 and not any(a <= cur and cur + W <= b for a, b in merged):
                missing.append(cur / 16000)
            i += 1
    return missing


def test_cap_cut_at_ended_pause_keeps_continuation_speech():
    # speech 0.5-20.0 s, pause 20.0-20.5 s, speech 20.5-29.8 s, silence: the
    # cap cuts in the ended pause and the 20.5-29.8 s speech must be emitted.
    pattern = [(Q, windows(500)), (S, windows(19500)), (Q, windows(500)), (S, windows(9300)),
               (Q, windows(2000))]
    chunks, _, _ = drive(pattern)
    assert [c.cut_reason for c in chunks] == ["cap", "silence"]
    assert speech_windows_covered(pattern, chunks) == []
    assert_no_overlap(chunks)


def test_every_speech_window_lands_in_a_chunk_random():
    import random

    rng = random.Random(11)
    for _ in range(30):
        pattern = []
        for _ in range(rng.randrange(5, 60)):
            pattern.append((S, rng.randrange(8, 700)))  # speech runs >= 256 ms are never dropped
            pattern.append((rng.choice([Q, M]), rng.randrange(1, 80)))
        pattern.append((Q, windows(2000)))
        chunks, ch, _ = drive(pattern)
        chunks.extend(ch.cut("shutdown"))
        assert_no_overlap(chunks)
        assert speech_windows_covered(pattern, chunks) == [], pattern
