import shutil
from pathlib import Path

import numpy as np
import pytest

from roomlog_edge import config as configmod

CONTRACT = Path(__file__).resolve().parents[2] / "contract"


def make_config(tmp_path: Path, **over) -> configmod.Config:
    data = {
        "device_id": "testbox",
        "server_url": "http://127.0.0.1:1",
        "token_file": str(tmp_path / "token"),
        "spool_dir": str(tmp_path / "spool"),
        "model_path": str(tmp_path / "silero_vad.onnx"),
        "status_dir": str(tmp_path / "run"),
        "metrics_file": str(tmp_path / "metrics.prom"),
    }
    data.update(over)
    (tmp_path / "token").write_text("secret-token\n")
    return configmod.from_dict(data)


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def contract_dir():
    return CONTRACT


@pytest.fixture
def encoder_available():
    if shutil.which("opusenc") is None and shutil.which("ffmpeg") is None:
        pytest.skip("no opusenc or ffmpeg on PATH")


def tone_pcm(seconds: float, freq: float = 440.0, rate: int = 16000, amp: int = 8000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.int16).tobytes()
