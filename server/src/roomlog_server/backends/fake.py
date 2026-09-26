"""`fake` backend: the same interface, no model. Used by the worker tests and `type = "fake"`
in config for end-to-end runs without weights.

Scripted mode: `FakeBackend(script=[Transcript, ...])` or `script=callable(audio, language)`.
Default mode: one segment per non-silent region of the audio, words every 0.5 s, so a
window of real chunks separated by silence maps back to the right chunks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import numpy as np

from ..config import BackendConfig
from .base import Backend, BackendUnavailable, Segment, Transcript, Word


def regions(audio: np.ndarray, sample_rate: int = 16000, min_gap_s: float = 0.2,
            eps: float = 1e-4) -> list[tuple[float, float]]:
    """Non-silent (|x| > eps) regions in seconds, merged across gaps under `min_gap_s`."""
    active = np.abs(audio) > eps
    if not active.any():
        return []
    idx = np.flatnonzero(active)
    out: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    min_gap = int(min_gap_s * sample_rate)
    for i in idx[1:]:
        i = int(i)
        if i - prev > min_gap:
            out.append((start, prev + 1))
            start = i
        prev = i
    out.append((start, prev + 1))
    return [(a / sample_rate, b / sample_rate) for a, b in out]


class FakeBackend(Backend):
    def __init__(self, cfg: BackendConfig | None = None, *,
                 script: Iterable[Transcript] | Callable[[np.ndarray, str | None], Transcript] | None = None,
                 supports_words: bool = True, healthy: bool = True, model_id: str = "fake-model",
                 name: str = "fake", fail: Exception | None = None) -> None:
        self.name = cfg.name if cfg else name
        self.model_id = (cfg.model if cfg and cfg.model else model_id)
        self.model_revision = "fake-rev"
        self.supports_words = bool(cfg.options.get("words", supports_words)) if cfg else supports_words
        self.max_concurrent = cfg.max_concurrent if cfg else 1
        self.healthy = healthy
        self.fail = fail
        self.calls: list[tuple[np.ndarray, str | None]] = []
        self._script_iter = None
        self._script_fn = None
        if callable(script):
            self._script_fn = script
        elif script is not None:
            self._script_iter = iter(script)
        super().__init__()

    def probe(self) -> bool:
        return self.healthy

    def transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        self.calls.append((audio, language))
        if self.fail is not None:
            raise self.fail
        if not self.healthy:
            raise BackendUnavailable(f"{self.name} down")
        if self._script_fn is not None:
            return self._script_fn(audio, language)
        if self._script_iter is not None:
            try:
                return next(self._script_iter)
            except StopIteration:
                raise BackendUnavailable("fake script exhausted") from None
        return self._default(audio, language)

    def _default(self, audio: np.ndarray, language: str | None) -> Transcript:
        segments: list[Segment] = []
        for n, (a, b) in enumerate(regions(audio)):
            words = None
            if self.supports_words:
                words = []
                t = a
                k = 0
                while t < b:
                    e = min(t + 0.5, b)
                    words.append(Word(t, e, f" ord{n}{k}", 0.9))
                    t = e
                    k += 1
            text = "".join(w.word for w in words) if words else f" region{n}"
            segments.append(Segment(a, b, text, words, avg_logprob=-0.3,
                                    compression_ratio=1.2, no_speech_prob=0.05))
        return Transcript(segments=segments, language=language or "no",
                          duration=len(audio) / 16000)
