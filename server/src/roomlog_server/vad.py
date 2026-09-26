"""Silero VAD v6.2.3 through onnxruntime (design §3.4).

The ONNX graph takes ``input`` [1, 64 + 512] float32 (64 samples of context
from the previous window followed by the current window), ``state`` [2, 1,
128] float32 and ``sr`` int64 scalar; it returns the speech probability and
the next state. onnxruntime is imported lazily so the module (and the test
suite, which uses a probability function instead) loads without it or the
model file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

WINDOW = 512
CONTEXT = 64
MODEL_NAME = "silero-vad"
MODEL_VERSION = "v6.2.3"
MODEL_GIT_BLOB_SHA1 = "80c5592ef1f4c9ede3e357bbd02eb863358a6a9d"


class Vad(Protocol):
    def __call__(self, window: np.ndarray) -> float: ...
    def reset(self) -> None: ...


class FunctionVad:
    """Adapter for tests: ``fn(window) -> probability``."""

    def __init__(self, fn: Callable[[np.ndarray], float]):
        self.fn = fn
        self.resets = 0

    def __call__(self, window: np.ndarray) -> float:
        return float(self.fn(window))

    def reset(self) -> None:
        self.resets += 1


class SileroVad:
    def __init__(self, model_path: str | Path, sample_rate: int = 16000, expected_sha256: str | None = None):
        import onnxruntime  # lazy: absent in the tests, heavy on the Pi

        path = Path(model_path)
        if expected_sha256:
            got = file_sha256(path)
            if got != expected_sha256:
                raise RuntimeError(f"model sha256 mismatch for {path}: {got} != {expected_sha256}")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(str(path), sess_options=opts,
                                                    providers=["CPUExecutionProvider"])
        self.sr = np.array(sample_rate, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        if window.shape != (WINDOW,):
            raise ValueError(f"window must have {WINDOW} samples, got {window.shape}")
        x = np.concatenate([self._context, window.reshape(1, WINDOW).astype(np.float32)], axis=1)
        out, state = self.session.run(None, {"input": x, "state": self._state, "sr": self.sr})
        self._state = state
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = Path(path).read_bytes()
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def pcm_to_float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def info(threshold: float) -> dict:
    return {"model": MODEL_NAME, "version": MODEL_VERSION, "threshold": threshold}
