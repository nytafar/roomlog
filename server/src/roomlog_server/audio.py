"""Audio helpers: Ogg Opus decode (PyAV, a faster-whisper dependency), WAV encode, windows.

All audio is float32 mono at 16 kHz, the shape faster-whisper takes directly.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


def decode_opus(path: str | Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any container PyAV understands to float32 mono at `sample_rate`."""
    import av  # PyAV is heavy; keep the import out of module load

    frames: list[np.ndarray] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                frames.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush
            frames.append(out.to_ndarray().reshape(-1))
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).astype(np.float32, copy=False)


def to_wav_bytes(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """16-bit PCM WAV, what OpenAI-compatible endpoints and whisper.cpp accept."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def from_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return pcm, rate


def concat_with_gaps(parts: list[np.ndarray], gap_s: float,
                     sample_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Join parts with `gap_s` of silence between them.

    Returns the window and, per part, `(offset_s, duration_s)` inside the window.
    """
    gap = np.zeros(int(round(gap_s * sample_rate)), dtype=np.float32)
    pieces: list[np.ndarray] = []
    spans: list[tuple[float, float]] = []
    pos = 0
    for i, part in enumerate(parts):
        if i:
            pieces.append(gap)
            pos += len(gap)
        spans.append((pos / sample_rate, len(part) / sample_rate))
        pieces.append(part)
        pos += len(part)
    if not pieces:
        return np.zeros(0, dtype=np.float32), spans
    return np.concatenate(pieces), spans


def synthetic_tone(seconds: float = 2.0, freq: float = 440.0,
                   sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """A quiet tone for `selftest` when no archive chunk exists yet."""
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.1 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
