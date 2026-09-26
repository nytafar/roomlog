"""Speech chunker: a pure state machine over (window index, probability).

Design §3.4. Windows are ``window`` samples long; window ``i`` spans absolute
samples ``[origin + i*window, origin + (i+1)*window)``. Output chunks are
half-open sample ranges ``[n_start, n_end)`` that never overlap.

Rules (defaults in samples at 16 kHz):
* hysteresis: speech starts at ``p >= threshold`` (0.5), ends at
  ``p < neg_threshold`` (0.35); values in between change nothing;
* padding ``pad`` (300 ms) on both sides, clipped to the previous chunk's end;
* a silence run shorter than ``min_silence`` (1500 ms) merges, one at least
  that long closes the chunk at ``silence_start + pad`` (``silence``);
* speech shorter than ``min_speech`` (250 ms) is dropped, except for the
  continuation after a cap cut, which is always kept;
* cap: when the padded chunk would reach ``max_len`` (30.0 s), cut at the
  midpoint of the longest internal pause of at least ``min_pause`` (100 ms),
  the pause in progress included; the next chunk starts at the cut point.
  With no such pause, hard cut at exactly ``max_len`` (``cap``);
* :meth:`cut` closes the open chunk at the end of the last window fed
  (``discontinuity`` or ``shutdown``). After a discontinuity the next chunk
  carries ``discontinuity=True`` and the caller sets a new ``origin``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chunk:
    n_start: int
    n_end: int
    cut_reason: str  # silence | cap | discontinuity | shutdown
    discontinuity: bool = False

    @property
    def n_samples(self) -> int:
        return self.n_end - self.n_start


@dataclass
class Chunker:
    rate: int = 16000
    window: int = 512
    threshold: float = 0.5
    neg_threshold: float = 0.35
    pad: int = 4800
    min_silence: int = 24000
    min_speech: int = 4000
    max_len: int = 480_000
    min_pause: int = 1600
    origin: int = 0

    _triggered: bool = False
    _chunk_start: int = 0  # padded start of the open chunk
    _speech_start: int = 0  # first speech sample of the open chunk
    _continued: bool = False  # open chunk is the tail after a cap cut
    _speech_seen: bool = False  # a speech window landed in the open chunk
    _temp_end: int | None = None  # start of the silence in progress
    _pauses: list[tuple[int, int]] = field(default_factory=list)  # (start, len)
    _prev_end: int = 0  # end of the last emitted chunk
    _last_end: int = 0  # end of the last window fed
    _next_discontinuity: bool = False

    @classmethod
    def from_ms(cls, rate: int = 16000, window: int = 512, threshold: float = 0.5,
                neg_threshold: float = 0.35, pad_ms: int = 300, min_silence_ms: int = 1500,
                min_speech_ms: int = 250, max_chunk_s: float = 30.0, min_pause_ms: int = 100,
                origin: int = 0) -> "Chunker":
        ms = rate // 1000
        return cls(rate=rate, window=window, threshold=threshold, neg_threshold=neg_threshold,
                   pad=pad_ms * ms, min_silence=min_silence_ms * ms, min_speech=min_speech_ms * ms,
                   max_len=int(round(max_chunk_s * rate)), min_pause=min_pause_ms * ms, origin=origin)

    # -- public ------------------------------------------------------------

    @property
    def open(self) -> bool:
        return self._triggered

    def reset(self, origin: int) -> None:
        """Forget all state and start a fresh sample timeline at ``origin``."""
        self.origin = origin
        self._triggered = False
        self._temp_end = None
        self._pauses = []
        self._prev_end = origin
        self._last_end = origin
        self._continued = False
        self._speech_seen = False

    def feed(self, index: int, prob: float) -> list[Chunk]:
        cur = self.origin + index * self.window
        end = cur + self.window
        self._last_end = end
        out: list[Chunk] = []

        if prob >= self.threshold:
            if self._temp_end is not None:
                pause = cur - self._temp_end
                if pause >= self.min_pause:
                    self._pauses.append((self._temp_end, pause))
                self._temp_end = None
            if not self._triggered:
                self._triggered = True
                self._speech_start = cur
                self._chunk_start = max(cur - self.pad, self._prev_end)
                self._continued = False
                self._pauses = []
            self._speech_seen = True
        elif self._triggered and prob < self.neg_threshold:
            if self._temp_end is None:
                self._temp_end = cur
            if end - self._temp_end >= self.min_silence:
                out.extend(self._close(self._temp_end + self.pad, "silence"))
                return out

        if self._triggered and end - self._chunk_start >= self.max_len:
            out.extend(self._cap_cut(end))
        return out

    def cut(self, reason: str) -> list[Chunk]:
        """Close the open chunk at the end of the last window (discontinuity
        or shutdown). Returns the chunk if any."""
        out: list[Chunk] = []
        if self._triggered:
            n_end = self._last_end
            if self._temp_end is not None:
                n_end = min(n_end, self._temp_end + self.pad)
            out.extend(self._close(n_end, reason))
        if reason == "discontinuity":
            self._next_discontinuity = True
        return out

    # -- internals ---------------------------------------------------------

    def _emit(self, n_start: int, n_end: int, reason: str) -> list[Chunk]:
        if n_end <= n_start:
            return []
        chunk = Chunk(n_start, n_end, reason, self._next_discontinuity)
        self._next_discontinuity = False
        self._prev_end = n_end
        return [chunk]

    def _close(self, n_end: int, reason: str) -> list[Chunk]:
        speech_end = self._temp_end if self._temp_end is not None else n_end
        if self._continued:
            keep = self._speech_seen
        else:
            keep = (speech_end - self._speech_start) >= self.min_speech
        out = self._emit(self._chunk_start, n_end, reason) if keep else []
        self._triggered = False
        self._temp_end = None
        self._pauses = []
        self._continued = False
        return out

    def _cap_cut(self, end: int) -> list[Chunk]:
        candidates = list(self._pauses)
        if self._temp_end is not None:
            candidates.append((self._temp_end, end - self._temp_end))
        candidates = [c for c in candidates
                      if c[1] >= self.min_pause and c[0] + c[1] // 2 > self._chunk_start]
        if candidates:
            start, length = max(candidates, key=lambda c: c[1])
            cut = start + length // 2
            out = self._emit(self._chunk_start, cut, "cap")
            # continue in the same pause if it is still running
            self._temp_end = cut if self._temp_end is not None and start == self._temp_end else None
        else:
            cut = self._chunk_start + self.max_len
            out = self._emit(self._chunk_start, cut, "cap")
            self._temp_end = None
        self._chunk_start = cut
        self._speech_start = cut
        self._continued = True
        self._speech_seen = False
        self._pauses = []
        return out
