"""The transcription backend interface (§4.4a).

    transcribe(audio, language) → Transcript (segments, with words when available)
    probe() → bool
    model_id, model_revision, supports_words

`BackendUnavailable` (connection error, timeout, 5xx, model missing) makes the router fall
through to the next backend; `BackendError` (4xx, bad response) does not.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    no_speech_prob: float | None = None


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    duration: float | None = None

    @property
    def has_words(self) -> bool:
        return bool(self.segments) and all(s.words for s in self.segments if s.text.strip())


class BackendUnavailable(Exception):
    """Transient: try the next backend."""


class BackendError(Exception):
    """Non-transient: the request itself is bad or the response unusable."""


class Backend:
    name: str = "backend"
    model_id: str = ""
    model_revision: str | None = None
    supports_words: bool = True
    max_concurrent: int = 1

    def __init__(self) -> None:
        self._sem = threading.BoundedSemaphore(max(1, self.max_concurrent))

    def probe(self) -> bool:
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        raise NotImplementedError

    def transcribe_limited(self, audio: np.ndarray, language: str | None) -> Transcript:
        with self._sem:
            return self.transcribe(audio, language)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name} model={self.model_id}>"
