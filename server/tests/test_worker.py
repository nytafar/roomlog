from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from roomlog_server.archive import insert_chunk_row
from roomlog_server.backends import BackendError, BackendUnavailable, Router, Segment, Transcript, Word
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.config import Filters
from roomlog_server.times import iso_to_ms, ms_to_iso
from roomlog_server.worker import (
    Chunk, Span, Worker, apply_filters, build_windows, map_transcript,
)

from conftest import make_config, make_sidecar

T0 = iso_to_ms("2026-09-26T10:00:00.000Z")
SR = 16000


class Fixture:
    """Chunks in the DB plus an in-memory 'archive' the worker decodes from."""

    def __init__(self, tmp_path, **cfg_overrides):
        self.cfg = make_config(tmp_path, **cfg_overrides)
        from roomlog_server import db as dbmod
        self.conn = dbmod.connect(self.cfg.db_path)
        self.audio: dict[str, np.ndarray] = {}
        self.n = 0

    def add(self, start_ms: int, duration_s: float, device_id: str = "oma",
            amplitude: float = 0.5) -> int:
        self.n += 1
        body = f"{device_id}-{start_ms}-{self.n}".encode()
        meta = make_sidecar(body, device_id=device_id, start_utc=ms_to_iso(start_ms),
                            duration_s=duration_s)
        path = f"p/{self.n}.opus"
        self.audio[str(self.cfg.archive_dir / path)] = np.full(int(duration_s * SR), amplitude, np.float32)
        return insert_chunk_row(self.conn, meta, json.dumps(meta), hashlib.sha256(body).hexdigest(), path)

    def decode(self, path: str) -> np.ndarray:
        if path not in self.audio:
            raise FileNotFoundError(path)
        return self.audio[path]

    def worker(self, *backends, now=None) -> Worker:
        router = Router(list(backends), probe_ttl_s=0)
        return Worker(self.cfg, self.conn, router, decode=self.decode, now=now or (lambda: T0 + 10_000_000))

    def chunk(self, cid: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone())

    def segments(self, cid: int | None = None) -> list[dict]:
        if cid is None:
            rows = self.conn.execute("SELECT * FROM segments ORDER BY chunk_id, idx")
        else:
            rows = self.conn.execute("SELECT * FROM segments WHERE chunk_id=? ORDER BY idx", (cid,))
        return [dict(r) for r in rows]


def chunk(i: int, start_s: float, dur_s: float, device="oma") -> Chunk:
    return Chunk(id=i, device_id=device, start_utc_ms=T0 + int(start_s * 1000),
                 end_utc_ms=T0 + int((start_s + dur_s) * 1000), duration_ms=int(dur_s * 1000),
                 path=f"p/{i}.opus")


# ------------------------------------------------------------- windowing


def test_window_packing_respects_30s_with_gaps():
    chunks = [chunk(i, i * 20, 10) for i in range(4)]  # 10 s each, 10 s apart
    windows = build_windows(chunks, window_s=30, gap_s=0.3, session_gap_s=300)
    # 10 + 0.3 + 10 = 20.3 fits; + 0.3 + 10 = 30.6 does not
    assert [[c.id for c in w] for w in windows] == [[0, 1], [2, 3]]
    chunks = [chunk(i, i * 20, 9) for i in range(4)]
    windows = build_windows(chunks, window_s=30, gap_s=0.3, session_gap_s=300)
    assert [[c.id for c in w] for w in windows] == [[0, 1, 2], [3]]  # 27.6 fits, 36.9 does not
    chunks = [chunk(0, 0, 29.8), chunk(1, 30, 0.1)]
    assert [[c.id for c in w] for w in build_windows(chunks)] == [[0], [1]]  # 29.8 + 0.3 + 0.1 > 30


def test_window_exact_fit_and_lone_oversize():
    chunks = [chunk(0, 0, 14.85), chunk(1, 20, 14.85)]  # 29.7 + 0.3 = 30.0 exactly
    assert [[c.id for c in w] for w in build_windows(chunks)] == [[0, 1]]
    chunks = [chunk(0, 0, 14.9), chunk(1, 20, 14.9)]  # 30.1 > 30
    assert [[c.id for c in w] for w in build_windows(chunks)] == [[0], [1]]
    big = [chunk(0, 0, 31.0)]
    assert [[c.id for c in w] for w in build_windows(big)] == [[0]]


def test_window_breaks_on_device_and_session_gap():
    chunks = [chunk(0, 0, 5), chunk(1, 100, 5), chunk(2, 100 + 5 + 300.001, 5),
              chunk(3, 800, 5, device="pi-work")]
    windows = build_windows(chunks, session_gap_s=300)
    assert [[c.id for c in w] for w in windows] == [[0, 1], [2], [3]]
    per = build_windows(chunks, per_chunk=True)
    assert [[c.id for c in w] for w in per] == [[0], [1], [2], [3]]


# ------------------------------------------------------------- mapping


def test_words_map_to_chunks_minutes_apart():
    a = chunk(1, 0, 10)          # T0 .. T0+10s
    b = chunk(2, 120, 5)         # T0+120s .. +125s
    spans = [Span(a, 0.0, 10.0), Span(b, 10.3, 5.0)]
    t = Transcript(segments=[
        Segment(1.0, 1.5, " hei", [Word(1.0, 1.5, " hei", 0.9)]),
        Segment(11.0, 11.5, " du", [Word(11.0, 11.5, " du", 0.9)]),
    ], language="no")
    mapped = map_transcript(t, spans)
    assert [(m.chunk.id, m.start_utc_ms - T0, m.end_utc_ms - T0, m.offset_ms, m.text) for m in mapped] == [
        (1, 1000, 1500, 1000, "hei"),
        (2, 120_000 + 700, 120_000 + 1200, 700, "du"),
    ]
    assert mapped[1].words == [{"w": " du", "s": 700, "e": 1200, "p": 0.9}]


def test_segment_spanning_boundary_is_split():
    a, b = chunk(1, 0, 10), chunk(2, 30, 10)
    spans = [Span(a, 0.0, 10.0), Span(b, 10.3, 10.0)]
    words = [Word(8.0, 8.5, " vi", 0.9), Word(8.6, 9.9, " går", 0.9),
             Word(10.4, 10.9, " hjem", 0.9), Word(11.0, 11.6, " nå", 0.9)]
    t = Transcript(segments=[Segment(8.0, 11.6, " vi går hjem nå", words)])
    mapped = map_transcript(t, spans)
    assert [(m.chunk.id, m.text) for m in mapped] == [(1, "vi går"), (2, "hjem nå")]
    assert (mapped[0].start_utc_ms - T0, mapped[0].end_utc_ms - T0) == (8000, 9900)
    assert (mapped[1].start_utc_ms - T0, mapped[1].end_utc_ms - T0) == (30_100, 31_300)
    assert mapped[1].offset_ms == 100


def test_unsplit_segment_keeps_original_text_and_clips_to_chunk():
    a = chunk(1, 0, 10)
    spans = [Span(a, 0.0, 10.0)]
    t = Transcript(segments=[Segment(-0.2, 10.4, "  Hei, verden!  ", [Word(0.0, 0.4, " Hei,", 0.9)])])
    m = map_transcript(t, spans)[0]
    assert m.text == "Hei, verden!"
    assert (m.offset_ms, m.end_utc_ms - m.chunk.start_utc_ms) == (0, 10_000)


def test_word_in_gap_goes_to_nearest_chunk_and_wordless_segment_uses_midpoint():
    a, b = chunk(1, 0, 10), chunk(2, 30, 10)
    spans = [Span(a, 0.0, 10.0), Span(b, 10.3, 10.0)]
    t = Transcript(segments=[
        Segment(9.9, 10.3, " x", [Word(10.2, 10.3, " x", 0.5)]),    # in the gap, nearer b
        Segment(12.0, 13.0, " y"),                                  # no words, midpoint in b
        Segment(0.0, 0.0, "   "),                                   # empty: skipped
    ])
    mapped = map_transcript(t, spans)
    assert [(m.chunk.id, m.text, m.offset_ms) for m in mapped] == [(2, "x", 0), (2, "y", 1700)]


def test_join_words_without_leading_spaces():
    a = chunk(1, 0, 10)
    b = chunk(2, 30, 10)
    spans = [Span(a, 0.0, 10.0), Span(b, 10.3, 10.0)]
    words = [Word(1, 2, "Hei", 0.9), Word(2, 3, "der,", 0.9), Word(11, 12, "du", 0.9)]
    mapped = map_transcript(Transcript([Segment(1, 12, "Hei der, du", words)]), spans)
    assert [m.text for m in mapped] == ["Hei der,", "du"]


# ------------------------------------------------------------- filters


def _m(text, **kw):
    from roomlog_server.worker import Mapped
    base = dict(chunk=chunk(1, 0, 10), start_utc_ms=T0, end_utc_ms=T0 + 1000, offset_ms=0,
                text=text, words=None, lang="no", avg_logprob=None, compression_ratio=None,
                no_speech_prob=None)
    base.update(kw)
    return Mapped(**base)


def test_filters():
    f = Filters()
    kept, dropped = apply_filters([
        _m("fin", avg_logprob=-0.5, compression_ratio=1.5, no_speech_prob=0.1),
        _m("logprob", avg_logprob=-1.01),
        _m("compress", compression_ratio=2.41),
        _m("nospeech", no_speech_prob=0.61),
        _m("Teksting av Nicolai Winther"),
        _m("takk for at du så på!"),
        _m("Thank you for watching."),
        _m("stats missing → kept"),
        _m("  "),
        _m("«Teksting av» in quotes"),
    ], f)
    assert [m.text for m in kept] == ["fin", "stats missing → kept"]
    assert dropped == {1: 8}
    # thresholds can be switched off
    off = Filters(min_avg_logprob=None, max_compression_ratio=None, max_no_speech_prob=None, blocklist=[])
    kept, _ = apply_filters([_m("x", avg_logprob=-5, compression_ratio=9, no_speech_prob=1.0),
                             _m("Teksting av")], off)
    assert len(kept) == 2


# ------------------------------------------------------------- the worker end to end (fake backend)


def scripted(*transcripts):
    return FakeBackend(script=list(transcripts))


def test_worker_batch_writes_segments_and_sessions(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    b = fx.add(T0 + 60_000, 5)
    backend = FakeBackend(model_id="fake-medium")
    w = fx.worker(backend)
    res = w.run_once()
    assert (res.claimed, res.done, res.failed, res.windows) == (2, 2, 0, 1)
    assert len(backend.calls) == 1
    joined, lang = backend.calls[0]
    assert len(joined) == 10 * SR + int(0.3 * SR) + 5 * SR
    assert lang == "no"
    ca, cb = fx.chunk(a), fx.chunk(b)
    assert ca["status"] == cb["status"] == "done"
    assert ca["model_id"] == "fake-medium" and ca["model_revision"] == "fake-rev"
    assert ca["transcribed_utc_ms"] == T0 + 10_000_000
    sa, sb = fx.segments(a), fx.segments(b)
    assert len(sa) == 1 and len(sb) == 1
    assert sa[0]["start_utc_ms"] == T0 and sa[0]["end_utc_ms"] == T0 + 10_000
    assert sb[0]["start_utc_ms"] == T0 + 60_000 and sb[0]["end_utc_ms"] == T0 + 65_000
    assert sb[0]["offset_ms"] == 0
    assert json.loads(sb[0]["words_json"])[0]["s"] == 0
    assert fx.conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert ca["session_id"] == "oma_20260926T100000000Z"
    assert fx.conn.execute("SELECT n_segments FROM sessions").fetchone()[0] == 2
    # nothing left to claim
    assert w.run_once().claimed == 0


def test_idempotent_rerun(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    backend = FakeBackend()
    w = fx.worker(backend)
    w.run_once()
    first = fx.segments(a)
    fx.conn.execute("UPDATE chunks SET status='pending'")
    w.run_once()
    second = fx.segments(a)
    assert len(first) == len(second) == 1
    assert first[0]["text"] == second[0]["text"]
    assert first[0]["id"] != second[0]["id"]
    n_fts = fx.conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH 'ord00'").fetchone()[0]
    assert n_fts == 1
    # a different model's segments are left alone
    other = FakeBackend(model_id="other-model")
    fx.conn.execute("UPDATE chunks SET status='pending'")
    fx.worker(other).run_once()
    assert len(fx.segments(a)) == 2
    assert fx.chunk(a)["model_id"] == "other-model"


def test_filters_count_drops_on_chunk(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    t = Transcript(segments=[
        Segment(0.5, 1.0, " Teksting av Nicolai", [Word(0.5, 1.0, " Teksting av Nicolai", 0.9)]),
        Segment(2.0, 3.0, " bra", [Word(2.0, 3.0, " bra", 0.9)], avg_logprob=-0.2),
        Segment(4.0, 5.0, " dårlig", [Word(4.0, 5.0, " dårlig", 0.1)], avg_logprob=-2.0),
    ])
    fx.worker(scripted(t)).run_once()
    assert [s["text"] for s in fx.segments(a)] == ["bra"]
    assert fx.chunk(a)["n_segments_dropped"] == 2


def test_failure_marking_and_max_attempts(tmp_path):
    """An attempt is spent only when every configured backend failed for the window.

    With a single backend a 4xx exhausts the list, so attempts accrue: pending until
    max_attempts, then failed. With a second healthy backend the same 4xx costs nothing
    (see test_failover.py).
    """
    fx = Fixture(tmp_path, max_attempts=3)
    a = fx.add(T0, 10)
    boom = FakeBackend(fail=BackendError("bad request", status=400))
    w = fx.worker(boom)
    for i in range(1, 3):
        res = w.run_once()
        assert res.failed == 1
        c = fx.chunk(a)
        assert (c["status"], c["attempts"]) == ("pending", i)
        assert c["error"].startswith("backends:") and "bad request" in c["error"]
    res = w.run_once()
    c = fx.chunk(a)
    assert (c["status"], c["attempts"]) == ("failed", 3)
    assert w.run_once().claimed == 0
    # the same 4xx with a healthy fallback: transcribed by the fallback, no attempt spent
    fx.conn.execute("UPDATE chunks SET status='pending', attempts=0")
    local = FakeBackend(name="local", model_id="nb-medium")
    fx.worker(boom, local).run_once()
    c = fx.chunk(a)
    assert (c["status"], c["attempts"], c["model_id"]) == ("done", 0, "nb-medium")


def test_all_backends_down_counts_attempt_but_no_backend_selected_does_not(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    # probes pass but every request fails transiently: an attempt is spent
    b1 = FakeBackend(name="b1", fail=BackendUnavailable("500"))
    b2 = FakeBackend(name="b2", fail=BackendUnavailable("conn refused"))
    fx.worker(b1, b2).run_once()
    c = fx.chunk(a)
    assert (c["status"], c["attempts"]) == ("pending", 1)
    assert "b1" in c["error"] and "b2" in c["error"]
    # nobody passes probe: chunks wait untouched
    fx.worker(FakeBackend(healthy=False)).run_once()
    assert fx.chunk(a)["attempts"] == 1


def test_failover_to_next_backend(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    down = FakeBackend(name="berget", model_id="nb-large", healthy=False)
    flaky = FakeBackend(name="mac", model_id="mac-large", fail=BackendUnavailable("asleep"))
    local = FakeBackend(name="local", model_id="nb-medium")
    fx.worker(down, flaky, local).run_once()
    assert down.calls == []
    assert len(flaky.calls) == 1
    assert len(local.calls) == 1
    assert fx.chunk(a)["model_id"] == "nb-medium"
    assert fx.segments(a)[0]["model_id"] == "nb-medium"


def test_decode_failure_marks_only_that_chunk(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    b = fx.add(T0 + 20_000, 10)
    del fx.audio[str(fx.cfg.archive_dir / "p/2.opus")]
    backend = FakeBackend()
    res = fx.worker(backend).run_once()
    assert (res.done, res.failed) == (1, 1)
    assert fx.chunk(a)["status"] == "done"
    cb = fx.chunk(b)
    assert (cb["status"], cb["attempts"]) == ("pending", 1)
    assert cb["error"].startswith("decode:")


def test_backend_without_words_runs_per_chunk(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    b = fx.add(T0 + 20_000, 10)
    backend = FakeBackend(supports_words=False)
    res = fx.worker(backend).run_once()
    assert res.windows == 2
    assert len(backend.calls) == 2
    assert all(len(call[0]) == 10 * SR for call in backend.calls)
    assert fx.segments(b)[0]["start_utc_ms"] == T0 + 20_000
    assert fx.segments(b)[0]["words_json"] is None


def test_wordless_response_from_word_backend_reruns_singly(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    b = fx.add(T0 + 20_000, 10)
    no_words = Transcript(segments=[Segment(0, 20.3, " alt i ett")])
    single_a = Transcript(segments=[Segment(0, 10, " a-del")])
    single_b = Transcript(segments=[Segment(0, 10, " b-del")])
    backend = FakeBackend(script=[no_words, single_a, single_b])
    fx.worker(backend).run_once()
    assert len(backend.calls) == 3
    assert fx.segments(a)[0]["text"] == "a-del"
    assert fx.segments(b)[0]["text"] == "b-del"
    assert fx.segments(b)[0]["start_utc_ms"] == T0 + 20_000


def test_claim_order_and_batch_size(tmp_path):
    fx = Fixture(tmp_path, batch_size=3)
    fx.add(T0 + 5_000, 1, device_id="pi-work")
    fx.add(T0 + 2_000, 1, device_id="oma")
    fx.add(T0 + 1_000, 1, device_id="oma")
    fx.add(T0 + 3_000, 1, device_id="oma")
    w = fx.worker(FakeBackend())
    claimed = w.claim()
    assert [(c.device_id, c.start_utc_ms - T0) for c in claimed] == [("oma", 1000), ("oma", 2000), ("oma", 3000)]


def test_language_auto_passes_none(tmp_path):
    fx = Fixture(tmp_path, language=None)
    fx.add(T0, 3)
    backend = FakeBackend()
    fx.worker(backend).run_once()
    assert backend.calls[0][1] is None
