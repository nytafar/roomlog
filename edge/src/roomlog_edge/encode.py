"""Opus encoding through a subprocess (design §3.5).

``opusenc`` is preferred (``--speech``, 24 kbps); ``ffmpeg -c:a libopus`` is
the fallback and what the workstation tests use. Input is raw 16 kHz mono
int16 PCM; output is the Ogg Opus bytes. Comment tags carry only immutable
facts (contract: ``ROOMLOG_DEVICE_ID``, ``ROOMLOG_RUN_ID``, ``ROOMLOG_EPOCH``,
``ROOMLOG_N_START``, ``ROOMLOG_EDGE_VERSION``).
"""

from __future__ import annotations

import shutil
import subprocess

BACKENDS = ("opusenc", "ffmpeg")


class EncoderError(RuntimeError):
    pass


def detect_backend(preferred: str = "auto") -> str:
    if preferred in BACKENDS:
        if shutil.which(preferred) is None:
            raise EncoderError(f"encoder backend {preferred!r} not found on PATH")
        return preferred
    if preferred != "auto":
        raise EncoderError(f"unknown encoder backend {preferred!r}")
    for b in BACKENDS:
        if shutil.which(b):
            return b
    raise EncoderError("no Opus encoder found (need opusenc or ffmpeg)")


def roomlog_tags(device_id: str, run_id: str, epoch: int, n_start: int, edge_version: str) -> dict[str, str]:
    return {
        "ROOMLOG_DEVICE_ID": device_id,
        "ROOMLOG_RUN_ID": run_id,
        "ROOMLOG_EPOCH": str(epoch),
        "ROOMLOG_N_START": str(n_start),
        "ROOMLOG_EDGE_VERSION": edge_version,
    }


class Encoder:
    def __init__(self, backend: str = "auto", bitrate_kbps: int = 24, sample_rate: int = 16000,
                 timeout_s: float = 120.0):
        self.backend = detect_backend(backend)
        self.bitrate_kbps = bitrate_kbps
        self.sample_rate = sample_rate
        self.timeout_s = timeout_s

    def command(self, tags: dict[str, str]) -> list[str]:
        if self.backend == "opusenc":
            cmd = ["opusenc", "--quiet", "--raw", "--raw-bits", "16", "--raw-rate", str(self.sample_rate),
                   "--raw-chan", "1", "--raw-endianness", "0", "--bitrate", str(self.bitrate_kbps),
                   "--speech"]
            for k, v in tags.items():
                cmd += ["--comment", f"{k}={v}"]
            return cmd + ["-", "-"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1", "-i", "pipe:0",
               "-c:a", "libopus", "-b:a", f"{self.bitrate_kbps}k", "-application", "voip",
               "-vbr", "on", "-frame_duration", "20"]
        for k, v in tags.items():
            cmd += ["-metadata", f"{k}={v}"]
        return cmd + ["-f", "ogg", "pipe:1"]

    def encode(self, pcm: bytes, tags: dict[str, str]) -> bytes:
        if len(pcm) % 2:
            raise EncoderError("pcm length is not a whole number of int16 samples")
        if not pcm:
            raise EncoderError("empty pcm")
        try:
            proc = subprocess.run(self.command(tags), input=pcm, capture_output=True,
                                  timeout=self.timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise EncoderError(f"{self.backend}: {e}") from e
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise EncoderError(f"{self.backend} exited {proc.returncode}: {err[-500:]}")
        out = proc.stdout
        if not out.startswith(b"OggS"):
            raise EncoderError(f"{self.backend} produced no Ogg stream")
        return out
