"""Transcription worker (§4.4).

Batch: claim pending chunks (device_id, start_utc_ms order, ≤ 50) → decode → pack windows
(≤ 30 s including 0.3 s gaps, consecutive chunks within the session gap) → transcribe
through the router → map words back to chunks → filter → write one transaction per window →
sessionize. A backend without word timestamps gets one chunk per request.

Dictation (ADR 0008): pending spans are applied before chunks are claimed (the span's text
becomes a `channel=dictation` row; STT rows already written for that time are marked
superseded). A chunk is claimed only once its end is `dictation_hold_s` old, so the span
usually wins. Audio inside a span is zeroed before STT, and a chunk with under
`MIN_UNCOVERED_S` of audio left skips STT altogether. Cancelled or empty spans leave the
audio to STT but put its rows on the dictation channel.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import sdnotify
from .audio import SAMPLE_RATE, concat_with_gaps, decode_opus
from .backends import Backend, NoBackendAvailable, Router, Segment, Transcript, Word
from .config import Config, Filters
from .segmenter import RawAudio, Segmenter, covering_segments, purge_raw
from .sessions import rebuild_sessions
from .spans import add_tag, span_tags, spans_overlapping
from .times import now_ms

log = logging.getLogger("roomlog.worker")

PURGE_INTERVAL_MS = 86_400_000  # the worker purges old speech-free raw audio once a day
SEGMENT_BUDGET_S = 60.0  # segmenter work per batch; a backlog continues in the next batch
DICTATION_MODEL_ID = "dictation"  # chunks.model_id for a chunk whose audio a span covered whole
MIN_UNCOVERED_S = 1.0  # less audio than this outside the spans: no STT call for the chunk


@dataclass
class Chunk:
    id: int
    device_id: str
    start_utc_ms: int
    end_utc_ms: int
    duration_ms: int
    path: str | None
    attempts: int = 0
    kind: str = "speech"
    run_id: str = ""
    epoch: int = 0
    n_start: int | None = None
    n_samples: int | None = None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Chunk":
        return cls(id=r["id"], device_id=r["device_id"], start_utc_ms=r["start_utc_ms"],
                   end_utc_ms=r["end_utc_ms"], duration_ms=r["duration_ms"], path=r["path"],
                   attempts=r["attempts"], kind=r["kind"], run_id=r["run_id"], epoch=r["epoch"],
                   n_start=r["n_start"], n_samples=r["n_samples"])


@dataclass
class Span:
    """A chunk's place inside a window: `offset_s` from the window start, `duration_s` long."""
    chunk: Chunk
    offset_s: float
    duration_s: float

    @property
    def end_s(self) -> float:
        return self.offset_s + self.duration_s


@dataclass
class Cover:
    """A chunk's overlap with dictation spans: sample ranges to zero before STT (the spans
    with text) and the cancelled spans whose STT rows go on the dictation channel."""
    masked: list[tuple[int, int]] = field(default_factory=list)
    text_spans: list[sqlite3.Row] = field(default_factory=list)
    cancelled_spans: list[sqlite3.Row] = field(default_factory=list)

    def masked_s(self) -> float:
        return sum(b - a for a, b in self.masked) / SAMPLE_RATE

    def holds(self, start_utc_ms: int, end_utc_ms: int) -> bool:
        """True when the midpoint of [start, end) lies inside a span with text."""
        mid = (start_utc_ms + end_utc_ms) / 2
        return any(sp["start_utc_ms"] <= mid < sp["end_utc_ms"] for sp in self.text_spans)


@dataclass
class Mapped:
    """One output segment, already attributed to a chunk, times absolute and chunk-relative."""
    chunk: Chunk
    start_utc_ms: int
    end_utc_ms: int
    offset_ms: int
    text: str
    words: list[dict] | None
    lang: str | None
    avg_logprob: float | None
    compression_ratio: float | None
    no_speech_prob: float | None


# ---------------------------------------------------------------- windowing


def build_windows(chunks: list[Chunk], window_s: float = 30.0, gap_s: float = 0.3,
                  session_gap_s: float = 300.0, per_chunk: bool = False) -> list[list[Chunk]]:
    """Greedy packing in claim order; a lone chunk always fits, even if over `window_s`."""
    windows: list[list[Chunk]] = []
    cur: list[Chunk] = []
    cur_dur = 0.0
    for c in chunks:
        d = c.duration_ms / 1000.0
        if cur and not per_chunk:
            prev = cur[-1]
            same_device = prev.device_id == c.device_id
            close = (c.start_utc_ms - prev.end_utc_ms) <= session_gap_s * 1000
            fits = cur_dur + gap_s * len(cur) + d <= window_s + 1e-9
            if same_device and close and fits:
                cur.append(c)
                cur_dur += d
                continue
        if cur:
            windows.append(cur)
        cur, cur_dur = [c], d
    if cur:
        windows.append(cur)
    return windows


# ---------------------------------------------------------------- mapping


def _span_for(t: float, spans: list[Span]) -> Span:
    for sp in spans:
        if sp.offset_s <= t < sp.end_s:
            return sp
    # in a gap or past the end: nearest edge
    return min(spans, key=lambda sp: min(abs(sp.offset_s - t), abs(sp.end_s - t)))


def _rel_ms(t: float, sp: Span) -> int:
    ms = int(round((t - sp.offset_s) * 1000))
    return max(0, min(ms, sp.chunk.duration_ms))


def _piece(sp: Span, start: float, end: float, text: str, words: list[Word] | None,
           seg: Segment, lang: str | None) -> Mapped:
    off_start = _rel_ms(start, sp)
    off_end = max(off_start, _rel_ms(end, sp))
    wjson = None
    if words:
        wjson = [
            {"w": w.word, "s": _rel_ms(w.start, sp), "e": _rel_ms(w.end, sp), "p": w.probability}
            for w in words
        ]
    return Mapped(
        chunk=sp.chunk,
        start_utc_ms=sp.chunk.start_utc_ms + off_start,
        end_utc_ms=sp.chunk.start_utc_ms + off_end,
        offset_ms=off_start,
        text=text.strip(),
        words=wjson,
        lang=lang,
        avg_logprob=seg.avg_logprob,
        compression_ratio=seg.compression_ratio,
        no_speech_prob=seg.no_speech_prob,
    )


def _join_words(words: list[Word]) -> str:
    out = ""
    for w in words:
        tok = w.word
        if out and not tok.startswith(" ") and not out.endswith(" "):
            out += " "
        out += tok
    return out.strip()


def map_transcript(transcript: Transcript, spans: list[Span]) -> list[Mapped]:
    """Attribute every segment (or word group) to the chunk whose span holds its midpoint.

    A segment whose words fall into two chunks is split at the boundary into two pieces.
    """
    if not spans:
        return []
    out: list[Mapped] = []
    lang = transcript.language
    for seg in transcript.segments:
        if not seg.text.strip():
            continue
        if seg.words:
            groups: list[tuple[Span, list[Word]]] = []
            for w in seg.words:
                sp = _span_for((w.start + w.end) / 2, spans)
                if groups and groups[-1][0] is sp:
                    groups[-1][1].append(w)
                else:
                    groups.append((sp, [w]))
            if len(groups) == 1:
                sp, words = groups[0]
                out.append(_piece(sp, seg.start, seg.end, seg.text, words, seg, lang))
            else:
                for sp, words in groups:
                    out.append(_piece(sp, words[0].start, words[-1].end, _join_words(words),
                                      words, seg, lang))
        else:
            sp = _span_for((seg.start + seg.end) / 2, spans)
            out.append(_piece(sp, seg.start, seg.end, seg.text, None, seg, lang))
    return out


# ---------------------------------------------------------------- filtering


def _normalize(text: str) -> str:
    return text.strip().lstrip("\"'«»„“”-–— ").casefold()


def is_boilerplate(text: str, blocklist: list[str]) -> bool:
    t = _normalize(text)
    return any(t.startswith(_normalize(b)) for b in blocklist if b.strip())


def keep_segment(m: Mapped, f: Filters) -> bool:
    if not m.text.strip():
        return False
    if f.min_avg_logprob is not None and m.avg_logprob is not None and m.avg_logprob < f.min_avg_logprob:
        return False
    if (f.max_compression_ratio is not None and m.compression_ratio is not None
            and m.compression_ratio > f.max_compression_ratio):
        return False
    if (f.max_no_speech_prob is not None and m.no_speech_prob is not None
            and m.no_speech_prob > f.max_no_speech_prob):
        return False
    if is_boilerplate(m.text, f.blocklist):
        return False
    return True


def apply_filters(mapped: list[Mapped], f: Filters) -> tuple[list[Mapped], dict[int, int]]:
    kept: list[Mapped] = []
    dropped: dict[int, int] = {}
    for m in mapped:
        if keep_segment(m, f):
            kept.append(m)
        else:
            dropped[m.chunk.id] = dropped.get(m.chunk.id, 0) + 1
    return kept, dropped


# ---------------------------------------------------------------- the worker


@dataclass
class BatchResult:
    claimed: int = 0
    done: int = 0
    failed: int = 0
    windows: int = 0
    segmented_epochs: int = 0
    segmented_chunks: int = 0
    segment_more: bool = False  # the segmenter stopped at its budget; more raw audio waits
    spans_applied: int = 0
    covered: int = 0  # chunks done without STT: a dictation span covered their audio
    errors: list[str] = field(default_factory=list)


class Worker:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, router: Router,
                 decode: Callable[[str], np.ndarray] = decode_opus,
                 now: Callable[[], int] = now_ms,
                 segmenter: Segmenter | None = None) -> None:
        self.cfg = cfg
        self.conn = conn
        self.router = router
        self.decode = decode
        self.now = now
        self.segmenter = segmenter
        assert cfg.archive_dir is not None
        self.archive_dir = cfg.archive_dir
        self._last_purge_ms: int | None = None

    # -- claiming

    def claim(self) -> list[Chunk]:
        """Pending chunks whose audio ended at least `dictation_hold_s` ago (ADR 0008)."""
        rows = self.conn.execute(
            """SELECT id, device_id, start_utc_ms, end_utc_ms, duration_ms, path, attempts,
                      kind, run_id, epoch, n_start, n_samples
               FROM chunks WHERE status = 'pending' AND attempts < ? AND end_utc_ms <= ?
               ORDER BY device_id, start_utc_ms, id LIMIT ?""",
            (self.cfg.max_attempts, self.now() - int(self.cfg.dictation_hold_s * 1000), self.cfg.batch_size),
        ).fetchall()
        return [Chunk.from_row(r) for r in rows]

    # -- dictation spans

    def _tag_dictation(self, segment_id: int, span: sqlite3.Row, ts: int) -> None:
        have = {r["key"] for r in self.conn.execute(
            "SELECT key FROM tags WHERE target = 'segment' AND segment_id = ?", (segment_id,))}
        for key, value in span_tags(span):
            if key not in have:
                add_tag(self.conn, key, value, span["origin"], segment_id=segment_id, now=ts)

    def apply_spans(self) -> int:
        """Materialize pending spans. A span with text becomes one `channel=dictation` row
        (attached to the first overlapping chunk, or to none yet) and supersedes the STT rows
        it overlaps; a cancelled span only moves overlapping STT rows onto the channel."""
        rows = self.conn.execute(
            "SELECT * FROM dictation_spans WHERE status = 'pending' ORDER BY device_id, start_utc_ms, id"
        ).fetchall()
        for span in rows:
            ts = self.now()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                seg_id: int | None = None
                overlapping = """device_id = ? AND span_id IS NULL AND superseded_by IS NULL
                                 AND start_utc_ms < ? AND end_utc_ms > ?"""
                args = (span["device_id"], span["end_utc_ms"], span["start_utc_ms"])
                if span["cancelled"]:
                    for r in self.conn.execute(f"SELECT id FROM segments WHERE {overlapping}", args).fetchall():
                        self._tag_dictation(r["id"], span, ts)
                else:
                    chunk = self.conn.execute(
                        """SELECT id, start_utc_ms FROM chunks
                           WHERE device_id = ? AND start_utc_ms < ? AND end_utc_ms > ?
                           ORDER BY start_utc_ms, id LIMIT 1""", args).fetchone()
                    offset = max(0, span["start_utc_ms"] - chunk["start_utc_ms"]) if chunk else 0
                    cur = self.conn.execute(
                        """INSERT INTO segments (chunk_id, device_id, span_id, idx, start_utc_ms, end_utc_ms,
                               offset_ms, text, lang, model_id, model_revision)
                           VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, NULL)""",
                        (chunk["id"] if chunk else None, span["device_id"], span["id"], span["start_utc_ms"],
                         span["end_utc_ms"], offset, span["text"].strip(), span["lang"], span["engine"]),
                    )
                    seg_id = int(cur.lastrowid)
                    self._tag_dictation(seg_id, span, ts)
                    self.conn.execute(
                        f"UPDATE segments SET superseded_by = ? WHERE {overlapping}", (span["id"], *args))
                self.conn.execute(
                    "UPDATE dictation_spans SET status = 'applied', applied_utc_ms = ?, segment_id = ? WHERE id = ?",
                    (ts, seg_id, span["id"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return len(rows)

    def cover_for(self, c: Chunk, n_samples: int) -> Cover:
        cover = Cover()
        for sp in spans_overlapping(self.conn, c.device_id, c.start_utc_ms, c.end_utc_ms):
            if sp["cancelled"]:
                cover.cancelled_spans.append(sp)
                continue
            a = max(0, (sp["start_utc_ms"] - c.start_utc_ms) * SAMPLE_RATE // 1000)
            b = min(n_samples, -(-(sp["end_utc_ms"] - c.start_utc_ms) * SAMPLE_RATE // 1000))
            if b > a:
                cover.masked.append((int(a), int(b)))
                cover.text_spans.append(sp)
        return cover

    def attach_span_rows(self, c: Chunk, spans: list[sqlite3.Row]) -> None:
        """A span row written before its audio arrived joins the first chunk that overlaps it."""
        for sp in spans:
            self.conn.execute(
                "UPDATE segments SET chunk_id = ?, offset_ms = ? WHERE span_id = ? AND chunk_id IS NULL",
                (c.id, max(0, sp["start_utc_ms"] - c.start_utc_ms), sp["id"]))

    def mark_covered(self, chunks: list[Chunk]) -> None:
        """Done without STT: the span rows already hold the transcript for this audio."""
        ts = self.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for c in chunks:
                self.conn.execute(
                    """UPDATE chunks SET status = 'done', error = NULL, transcribed_utc_ms = ?,
                           model_id = ?, model_revision = NULL, n_segments_dropped = 0 WHERE id = ?""",
                    (ts, DICTATION_MODEL_ID, c.id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # -- audio

    def audio_for(self, c: Chunk, raw: RawAudio | None = None) -> np.ndarray:
        """The one seam between chunk kinds: a file for speech chunks, a slice of the
        decoded raw segments for derived ones (`raw` caches decodes across a batch)."""
        if c.kind == "derived":
            assert c.n_start is not None and c.n_samples is not None
            raw = raw or RawAudio(self.archive_dir, self.decode)
            n_end = c.n_start + c.n_samples
            segs = covering_segments(self.conn, c.device_id, c.run_id, c.epoch, c.n_start, n_end)
            return raw.span(segs, c.n_start, n_end)
        assert c.path is not None
        return self.decode(str(self.archive_dir / c.path))

    # -- raw segments

    def segment_raw(self, res: BatchResult) -> None:
        """One segmenter call, capped at `SEGMENT_BUDGET_S` so a raw backlog (about 25 s of
        work per hour of audio) neither starves speech chunks nor outlasts the watchdog."""
        if self.segmenter is None:
            return
        try:
            seg = self.segmenter.run_once(budget_s=SEGMENT_BUDGET_S)
        except Exception as e:
            log.exception("segmenter crashed")
            res.errors.append(f"segmenter: {e.__class__.__name__}: {e}")
            return
        res.segmented_epochs = seg.epochs
        res.segmented_chunks = seg.chunks
        res.segment_more = seg.more
        res.errors.extend(f"segmenter: {e}" for e in seg.errors)

    def maybe_purge_raw(self) -> dict[str, int] | None:
        """Run `purge_raw` at most once per `PURGE_INTERVAL_MS`; the loop calls this every poll."""
        now = self.now()
        if self._last_purge_ms is not None and now - self._last_purge_ms < PURGE_INTERVAL_MS:
            return None
        self._last_purge_ms = now
        out = purge_raw(self.conn, self.archive_dir, now=now)
        if out["deleted"]:
            log.info("purged %d raw segments (%.1f MB) older than the retention", out["deleted"], out["bytes"] / 1e6)
        return out

    # -- marking

    def mark_attempt_failed(self, chunks: list[Chunk], error: str) -> None:
        error = error[:1000]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for c in chunks:
                attempts = c.attempts + 1
                status = "failed" if attempts >= self.cfg.max_attempts else "pending"
                self.conn.execute(
                    "UPDATE chunks SET attempts = ?, status = ?, error = ? WHERE id = ?",
                    (attempts, status, error, c.id),
                )
                c.attempts = attempts
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def write_window(self, window: list[Chunk], mapped: list[Mapped], dropped: dict[int, int],
                     backend_by_chunk: dict[int, Backend], covers: dict[int, Cover] | None = None) -> None:
        """Replace each chunk's segments for the backend that produced them and mark it done.
        Rows inside a cancelled span go on the dictation channel (`covers`)."""
        by_chunk: dict[int, list[Mapped]] = {c.id: [] for c in window}
        for m in mapped:
            by_chunk[m.chunk.id].append(m)
        covers = covers or {}
        ts = self.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for c in window:
                backend = backend_by_chunk[c.id]
                self.conn.execute("DELETE FROM segments WHERE chunk_id = ? AND model_id = ? AND span_id IS NULL",
                                  (c.id, backend.model_id))
                pieces = sorted(by_chunk[c.id], key=lambda m: (m.start_utc_ms, m.end_utc_ms))
                for idx, m in enumerate(pieces):
                    cur = self.conn.execute(
                        """INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms,
                               text, lang, avg_logprob, no_speech_prob, compression_ratio,
                               words_json, model_id, model_revision)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (c.id, c.device_id, idx, m.start_utc_ms, m.end_utc_ms, m.offset_ms, m.text,
                         m.lang or self.cfg.language or "und",
                         m.avg_logprob, m.no_speech_prob, m.compression_ratio,
                         json.dumps(m.words, ensure_ascii=False) if m.words else None,
                         backend.model_id, backend.model_revision),
                    )
                    seg_id = int(cur.lastrowid)
                    for sp in covers.get(c.id, Cover()).cancelled_spans:
                        if m.start_utc_ms < sp["end_utc_ms"] and m.end_utc_ms > sp["start_utc_ms"]:
                            self._tag_dictation(seg_id, sp, ts)
                self.conn.execute(
                    """UPDATE chunks SET status = 'done', error = NULL, transcribed_utc_ms = ?,
                           model_id = ?, model_revision = ?, n_segments_dropped = ?
                       WHERE id = ?""",
                    (ts, backend.model_id, backend.model_revision, dropped.get(c.id, 0), c.id),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # -- one window

    def transcribe_window(self, window: list[Chunk], audio: dict[int, np.ndarray],
                          backend: Backend) -> tuple[list[Mapped], dict[int, Backend]]:
        """Returns the mapped segments and, per chunk id, the backend that produced its text."""
        parts = [audio[c.id] for c in window]
        joined, offsets = concat_with_gaps(parts, self.cfg.window_gap_s)
        spans = [Span(c, off, dur) for c, (off, dur) in zip(window, offsets)]
        transcript, used = self.router.transcribe(joined, self.cfg.language, start_at=backend)
        no_words = (not used.supports_words) or (bool(transcript.segments) and not transcript.has_words)
        if len(window) > 1 and no_words:
            # No word timestamps for a multi-chunk window: redo it one chunk per request so
            # the mapping is exact without words. Failover may switch backend mid-window, so
            # every chunk remembers who transcribed it.
            log.info("backend %s returned no words; re-running %d chunks singly", used.name, len(window))
            mapped: list[Mapped] = []
            by_chunk: dict[int, Backend] = {}
            for c in window:
                sdnotify.notify("WATCHDOG=1")
                t, used = self.router.transcribe(audio[c.id], self.cfg.language, start_at=used)
                by_chunk[c.id] = used
                mapped.extend(map_transcript(t, [Span(c, 0.0, len(audio[c.id]) / SAMPLE_RATE)]))
            return mapped, by_chunk
        return map_transcript(transcript, spans), {c.id: used for c in window}

    # -- one batch

    def run_once(self) -> BatchResult:
        res = BatchResult()
        self.segment_raw(res)
        try:
            res.spans_applied = self.apply_spans()
        except Exception as e:
            log.exception("applying dictation spans failed")
            res.errors.append(f"spans: {e.__class__.__name__}: {e}")
        chunks = self.claim()
        res.claimed = len(chunks)
        if not chunks:
            if res.spans_applied:
                rebuild_sessions(self.conn, self.cfg.session_gap_s, now=self.now())
            return res

        audio: dict[int, np.ndarray] = {}
        covers: dict[int, Cover] = {}
        ok: list[Chunk] = []
        covered: list[Chunk] = []
        raw = RawAudio(self.archive_dir, self.decode)
        for c in chunks:
            try:
                a = self.audio_for(c, raw)
                if len(a) == 0:
                    raise ValueError("decoded to zero samples")
            except Exception as e:
                log.warning("decode failed for chunk %d (%s): %s", c.id, c.path or f"derived n={c.n_start}", e)
                self.mark_attempt_failed([c], f"decode: {e}")
                res.failed += 1
                res.errors.append(f"chunk {c.id}: decode: {e}")
                continue
            cover = self.cover_for(c, len(a))
            if cover.masked:
                self.attach_span_rows(c, cover.text_spans)
                if len(a) / SAMPLE_RATE - cover.masked_s() < MIN_UNCOVERED_S:
                    covered.append(c)
                    continue
                a = a.copy()
                for lo, hi in cover.masked:
                    a[lo:hi] = 0.0
            audio[c.id] = a
            covers[c.id] = cover
            ok.append(c)
        if covered:
            self.mark_covered(covered)
            res.done += len(covered)
            res.covered = len(covered)
        if not ok:
            rebuild_sessions(self.conn, self.cfg.session_gap_s, now=self.now())
            return res

        backend = self.router.select()
        if backend is None:
            log.warning("no transcription backend available; %d chunks wait", len(ok))
            res.errors.append("no backend available")
            return res

        windows = build_windows(ok, self.cfg.window_s, self.cfg.window_gap_s,
                                self.cfg.session_gap_s, per_chunk=not backend.supports_words)
        res.windows = len(windows)
        for window in windows:
            sdnotify.notify("WATCHDOG=1")  # a 50-chunk backlog can outlast WatchdogSec
            try:
                mapped, by_chunk = self.transcribe_window(window, audio, backend)
                # STT leaking across a zeroed span: whatever lands inside it is the span's
                mapped = [m for m in mapped if not covers[m.chunk.id].holds(m.start_utc_ms, m.end_utc_ms)]
                kept, dropped = apply_filters(mapped, self.cfg.filters)
                self.write_window(window, kept, dropped, by_chunk, covers)
                res.done += len(window)
            except NoBackendAvailable as e:
                log.error("window of %d chunks: every backend failed: %s", len(window), e)
                self.mark_attempt_failed(window, f"backends: {e}")
                res.failed += len(window)
                res.errors.append(str(e))
            except Exception as e:
                log.exception("window of %d chunks failed", len(window))
                self.mark_attempt_failed(window, f"{e.__class__.__name__}: {e}")
                res.failed += len(window)
                res.errors.append(f"{e.__class__.__name__}: {e}")
        rebuild_sessions(self.conn, self.cfg.session_gap_s, now=self.now())
        return res

    # -- the loop

    def run_forever(self, stop: threading.Event | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        stop = stop or threading.Event()
        sdnotify.notify("READY=1")
        while not stop.is_set():
            sdnotify.notify("WATCHDOG=1")
            try:
                self.maybe_purge_raw()
                res = self.run_once()
                if res.segmented_chunks:
                    log.info("segmented %d raw epochs into %d chunks", res.segmented_epochs, res.segmented_chunks)
                if res.spans_applied:
                    log.info("applied %d dictation spans", res.spans_applied)
                if res.claimed:
                    log.info("batch: claimed=%d done=%d failed=%d windows=%d covered=%d",
                             res.claimed, res.done, res.failed, res.windows, res.covered)
                    sdnotify.notify("WATCHDOG=1")
                    if res.done and res.claimed >= self.cfg.batch_size:
                        continue  # backlog: go straight to the next batch
                if res.segment_more:
                    continue  # raw backlog: the segmenter stopped at its budget
            except Exception:
                log.exception("worker batch crashed")
            stop.wait(self.cfg.poll_s)


def requeue(conn: sqlite3.Connection, max_attempts: int, failed_only: bool = False,
            device_id: str | None = None, since_ms: int | None = None) -> int:
    """Put `failed` rows (and stranded `pending` rows with attempts exhausted) back in the queue.

    Returns the number of chunks requeued. `failed_only` leaves stranded pending rows alone.
    """
    if failed_only:
        where = ["status = 'failed'"]
        params: list[object] = []
    else:
        where = ["(status = 'failed' OR (status = 'pending' AND attempts >= ?))"]
        params = [max_attempts]
    if device_id:
        where.append("device_id = ?")
        params.append(device_id)
    if since_ms is not None:
        where.append("start_utc_ms >= ?")
        params.append(since_ms)
    cur = conn.execute(
        f"UPDATE chunks SET status = 'pending', attempts = 0, error = NULL WHERE {' AND '.join(where)}",
        params,
    )
    return cur.rowcount
