"""`local` backend: faster-whisper in-process, loaded lazily on first use (§4.4a).

    WhisperModel(<ct2 dir>, device="cpu", compute_type="int8")
    transcribe(..., beam_size=5, condition_on_previous_text=False, word_timestamps=True,
               vad_filter=False)

Nothing here imports faster_whisper at module load; `probe()` only checks the model dir.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from ..config import BackendConfig, Config
from .base import Backend, BackendUnavailable, Segment, Transcript, Word

REVISION_FILE = "REVISION"


def model_dir_for(cfg: Config, bcfg: BackendConfig) -> Path:
    if bcfg.options.get("model_dir"):
        return Path(bcfg.options["model_dir"]).expanduser()
    return cfg.local_model_dir(bcfg.model) / "ct2"


class LocalBackend(Backend):
    supports_words = True

    def __init__(self, bcfg: BackendConfig, cfg: Config) -> None:
        self.name = bcfg.name
        self.model_id = bcfg.model
        self.model_dir = model_dir_for(cfg, bcfg)
        self.compute_type = bcfg.compute_type
        self.device = bcfg.device
        self.beam_size = bcfg.beam_size
        self.max_concurrent = bcfg.max_concurrent
        self.options = dict(bcfg.options)
        self._model = None
        self._lock = threading.Lock()
        rev = self.model_dir.parent / REVISION_FILE
        self.model_revision = rev.read_text().strip() if rev.exists() else None
        super().__init__()

    def probe(self) -> bool:
        return (self.model_dir / "model.bin").exists()

    def _load(self):
        with self._lock:
            if self._model is None:
                if not self.probe():
                    raise BackendUnavailable(
                        f"local model missing at {self.model_dir}; run `roomlog fetch-model`"
                    )
                from faster_whisper import WhisperModel  # heavy import, on demand

                self._model = WhisperModel(
                    str(self.model_dir), device=self.device, compute_type=self.compute_type,
                    cpu_threads=int(self.options.get("cpu_threads", 0)),
                )
            return self._model

    def transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        model = self._load()
        segments_iter, info = model.transcribe(
            audio.astype(np.float32, copy=False),
            language=language,
            beam_size=self.beam_size,
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=False,
        )
        segments: list[Segment] = []
        for s in segments_iter:
            words = None
            if s.words:
                words = [Word(w.start, w.end, w.word, w.probability) for w in s.words]
            segments.append(Segment(
                start=s.start, end=s.end, text=s.text, words=words,
                avg_logprob=s.avg_logprob, compression_ratio=s.compression_ratio,
                no_speech_prob=s.no_speech_prob,
            ))
        return Transcript(segments=segments, language=info.language, duration=info.duration)
