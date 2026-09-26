from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from roomlog_server import db as dbmod
from roomlog_server.config import BackendConfig, Config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO_ROOT / "contract"


def make_config(tmp_path: Path, **overrides) -> Config:
    kwargs = dict(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        bind="127.0.0.1:0",
        backends=[BackendConfig(name="fake", type="fake", model="fake-model")],
    )
    kwargs.update(overrides)
    cfg = Config(**kwargs)
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def make_sidecar(body: bytes, device_id: str = "oma", start_utc: str = "2026-09-26T10:15:32.417Z",
                 duration_s: float = 12.352, **extra) -> dict:
    meta = {
        "schema_version": 1,
        "device_id": device_id,
        "sha256": hashlib.sha256(body).hexdigest(),
        "start_utc": start_utc,
        "duration_s": duration_s,
        "sample_rate": 16000,
        "run_id": str(uuid.uuid4()),
        "epoch": 0,
        "discontinuity": False,
        "clock_step": False,
        "clock_synced": True,
        "cut_reason": "silence",
        "edge_version": "0.1.0",
        "session_hint": None,
        "multi_speaker": None,
    }
    meta.update(extra)
    return meta


def meta_header(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=True, separators=(",", ":"))


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def conn(cfg: Config):
    c = dbmod.connect(cfg.db_path)
    yield c
    c.close()
