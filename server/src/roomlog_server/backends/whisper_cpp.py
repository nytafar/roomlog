"""`whisper_cpp` backend: whisper.cpp's `server` (`POST /inference`), segments only.

No word timestamps, so the worker feeds it one chunk per request (per-chunk mode).
"""

from __future__ import annotations

import numpy as np

from ..audio import to_wav_bytes
from ..config import BackendConfig
from .base import Backend, BackendError, Segment, Transcript
from .http import get_json, post_multipart


def parse_whisper_cpp(payload: dict) -> Transcript:
    if not isinstance(payload, dict):
        raise BackendError("whisper.cpp response is not an object")
    segments: list[Segment] = []
    for seg in payload.get("segments") or []:
        if "start" in seg and "end" in seg:
            start, end = float(seg["start"]), float(seg["end"])
        else:  # `timestamps: {from, to}` in "hh:mm:ss,mmm" or `t0/t1` in centiseconds
            start = float(seg.get("t0", 0)) / 100.0
            end = float(seg.get("t1", 0)) / 100.0
        segments.append(Segment(
            start=start, end=end, text=str(seg.get("text", "")),
            avg_logprob=seg.get("avg_logprob"),
            compression_ratio=seg.get("compression_ratio"),
            no_speech_prob=seg.get("no_speech_prob"),
        ))
    if not segments and payload.get("text"):
        segments.append(Segment(0.0, float(payload.get("duration") or 0.0), str(payload["text"])))
    return Transcript(segments=segments, language=payload.get("language"),
                      duration=payload.get("duration"))


class WhisperCppBackend(Backend):
    supports_words = False

    def __init__(self, cfg: BackendConfig) -> None:
        self.name = cfg.name
        self.model_id = cfg.model or "whisper.cpp"
        self.model_revision = None
        self.base_url = cfg.base_url.rstrip("/")
        self.timeout = cfg.timeout_s
        self.max_concurrent = cfg.max_concurrent
        self.extra_fields = dict(cfg.extra_fields)
        super().__init__()

    def probe(self) -> bool:
        try:
            get_json(f"{self.base_url}/", timeout=min(self.timeout, 15))
            return True
        except BackendError:
            return True
        except Exception:
            return False

    def transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        fields = {"response_format": "verbose_json", "temperature": "0.0"}
        if language:
            fields["language"] = language
        fields.update(self.extra_fields)
        payload = post_multipart(f"{self.base_url}/inference", fields, to_wav_bytes(audio),
                                 "audio.wav", timeout=self.timeout)
        return parse_whisper_cpp(payload)
