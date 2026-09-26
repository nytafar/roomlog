"""`openai` backend: any OpenAI-compatible `/audio/transcriptions` (Berget first, §4.4a).

Uploads WAV (Berget does not take Ogg), asks for `verbose_json`, adds per-backend
`extra_fields` (Berget: `align=true` → flat `words` with `word/start/end/score`). Words are
attached to segments by time; segments carry no logprob/compression/no-speech stats, so
those filters only apply when a provider returns them.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..audio import to_wav_bytes
from ..config import BackendConfig
from .base import Backend, BackendError, Segment, Transcript, Word
from .http import get_json, post_multipart


def attach_words(segments: list[Segment], words: list[Word]) -> None:
    """Put each word into the segment containing its midpoint, else the nearest one."""
    if not segments or not words:
        return
    for s in segments:
        s.words = []
    for w in words:
        mid = (w.start + w.end) / 2
        home = None
        for s in segments:
            if s.start <= mid <= s.end:
                home = s
                break
        if home is None:
            home = min(segments, key=lambda s: min(abs(s.start - mid), abs(s.end - mid)))
        assert home.words is not None
        home.words.append(w)
    for s in segments:
        if not s.words:
            s.words = None


def parse_verbose_json(payload: dict) -> Transcript:
    if not isinstance(payload, dict):
        raise BackendError("verbose_json response is not an object")
    segments: list[Segment] = []
    for seg in payload.get("segments") or []:
        segments.append(
            Segment(
                start=float(seg["start"]),
                end=float(seg["end"]),
                text=str(seg.get("text", "")),
                avg_logprob=seg.get("avg_logprob"),
                compression_ratio=seg.get("compression_ratio"),
                no_speech_prob=seg.get("no_speech_prob"),
            )
        )
    if not segments and payload.get("text"):
        dur = payload.get("duration")
        segments.append(Segment(start=0.0, end=float(dur or 0.0), text=str(payload["text"])))
    words: list[Word] = []
    for w in payload.get("words") or []:
        if "start" not in w or "end" not in w:
            continue
        prob = w.get("probability", w.get("score"))
        words.append(Word(start=float(w["start"]), end=float(w["end"]),
                          word=str(w.get("word", "")), probability=prob))
    # Some providers nest words under each segment instead of a flat array.
    if not words:
        for seg, s in zip(payload.get("segments") or [], segments):
            if seg.get("words"):
                s.words = [
                    Word(float(w["start"]), float(w["end"]), str(w.get("word", "")),
                         w.get("probability", w.get("score")))
                    for w in seg["words"] if "start" in w and "end" in w
                ]
    else:
        attach_words(segments, words)
    return Transcript(segments=segments, language=payload.get("language"),
                      duration=payload.get("duration"))


class OpenAICompatBackend(Backend):
    supports_words = True

    def __init__(self, cfg: BackendConfig) -> None:
        self.name = cfg.name
        self.model_id = cfg.model
        self.model_revision = None
        self.base_url = cfg.base_url.rstrip("/")
        self.api_key_file = cfg.api_key_file
        self.extra_fields = dict(cfg.extra_fields)
        self.timeout = cfg.timeout_s
        self.max_concurrent = cfg.max_concurrent
        self.supports_words = bool(cfg.options.get("words", True))
        self._key: str | None = None
        super().__init__()

    def _headers(self) -> dict[str, str]:
        if self._key is None:
            self._key = ""
            if self.api_key_file:
                p = Path(os.path.expanduser(self.api_key_file))
                if p.exists():
                    self._key = p.read_text().strip()
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def probe(self) -> bool:
        if self.api_key_file and not self._headers():
            return False  # key file configured but missing or empty: skip, do not burn attempts
        try:
            get_json(f"{self.base_url}/models", headers=self._headers(), timeout=min(self.timeout, 15))
            return True
        except BackendError as e:
            # The server answered. 401/403 means the key is wrong: treat as unavailable so the
            # next backend takes over; any other 4xx (e.g. no /models route) counts as reachable.
            return e.status not in (401, 403)
        except Exception:
            return False

    def transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        fields = {"model": self.model_id, "response_format": "verbose_json"}
        if language:
            fields["language"] = language
        fields.update(self.extra_fields)
        payload = post_multipart(
            f"{self.base_url}/audio/transcriptions", fields, to_wav_bytes(audio), "audio.wav",
            headers=self._headers(), timeout=self.timeout,
        )
        return parse_verbose_json(payload)
