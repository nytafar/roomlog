from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server.archive import insert_raw_row, raw_relpath
from roomlog_server.config import BackendConfig, Config
from roomlog_server.times import iso_to_ms, ms_to_iso

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


def make_raw_sidecar(body: bytes, device_id: str = "s22", start_utc: str = "2026-09-26T13:00:00.000Z",
                     n_start: int = 0, n_samples: int = 480_000, cut_reason: str = "cap",
                     **extra) -> dict:
    """A `kind: raw` sidecar shaped like `contract/examples/raw-full.json`."""
    meta = {
        "schema_version": 1,
        "kind": "raw",
        "device_id": device_id,
        "sha256": hashlib.sha256(body).hexdigest(),
        "start_utc": start_utc,
        "duration_s": round(n_samples / 16000, 3),
        "sample_rate": 16000,
        "run_id": "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b",
        "epoch": 0,
        "n_start": n_start,
        "n_samples": n_samples,
        "discontinuity": False,
        "clock_step": False,
        "clock_synced": True,
        "cut_reason": cut_reason,
        "edge_version": "android-0.1.0",
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


# ---------------------------------------------------------------- raw segments in the archive

RATE = 16000
SEGMENT = 480_000
RAW_T0 = iso_to_ms("2026-09-26T13:00:00.000Z")


def ffmpeg_required() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is the workstation Opus encoder")


def encode_opus(pcm: np.ndarray) -> bytes:
    """int16 PCM → Ogg Opus bytes the way the edge's ffmpeg backend does it."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
           "-c:a", "libopus", "-b:a", "24k", "-application", "voip", "-vbr", "on",
           "-frame_duration", "20", "-f", "ogg", "pipe:1"]
    return subprocess.run(cmd, input=pcm.astype("<i2").tobytes(), capture_output=True, check=True).stdout


def pattern_pcm(pattern: list[tuple[str, float]], amp: int = 8000) -> np.ndarray:
    """`[("s", 25.0), ("t", 15.0), ...]`: s = silence, t = 440 Hz tone, seconds each."""
    parts = []
    for kind, seconds in pattern:
        n = int(round(seconds * RATE))
        if kind == "t":
            t = np.arange(n) / RATE
            parts.append((np.sin(2 * np.pi * 440 * t) * amp).astype(np.int16))
        else:
            parts.append(np.zeros(n, dtype=np.int16))
    return np.concatenate(parts)


class RawSegments:
    """Cut a PCM stream into raw segments, encode them, and store them like ingest would."""

    def __init__(self, cfg: Config, conn, device_id: str = "s22", run_id: str | None = None,
                 epoch: int = 0, epoch_start_n: int = 0, start_utc_ms: int = RAW_T0,
                 received_utc_ms: int | None = None, discontinuity: bool = False,
                 clock_synced: bool = True) -> None:
        self.cfg, self.conn = cfg, conn
        self.device_id = device_id
        self.run_id = run_id or str(uuid.uuid4())
        self.epoch = epoch
        self.n0 = epoch_start_n
        self.start_utc_ms = start_utc_ms
        self.received_utc_ms = received_utc_ms if received_utc_ms is not None else start_utc_ms + 40_000
        self.discontinuity = discontinuity
        self.clock_synced = clock_synced
        self.metas: list[dict] = []

    def plan(self, pcm: np.ndarray, last_cut: str = "shutdown") -> list[tuple[int, np.ndarray, str]]:
        starts = list(range(0, len(pcm), SEGMENT))
        return [(self.n0 + i, pcm[i:i + SEGMENT], last_cut if i == starts[-1] else "cap") for i in starts]

    def store(self, n_start: int, piece: np.ndarray, cut_reason: str,
              received_utc_ms: int | None = None) -> dict:
        body = encode_opus(piece)
        offset_ms = (n_start - self.n0) * 1000 // RATE
        meta = make_raw_sidecar(body, device_id=self.device_id, start_utc=ms_to_iso(self.start_utc_ms + offset_ms),
                                n_start=n_start, n_samples=len(piece), cut_reason=cut_reason,
                                run_id=self.run_id, epoch=self.epoch,
                                discontinuity=self.discontinuity and n_start == self.n0,
                                clock_synced=self.clock_synced)
        rel = raw_relpath(iso_to_ms(meta["start_utc"]), meta["sha256"])
        opus = self.cfg.archive_dir / rel
        opus.parent.mkdir(parents=True, exist_ok=True)
        opus.write_bytes(body)
        opus.with_suffix(".json").write_text(meta_header(meta))
        insert_raw_row(self.conn, meta, meta_header(meta), meta["sha256"], rel,
                       received_utc_ms if received_utc_ms is not None else self.received_utc_ms)
        self.metas.append(meta)
        return meta

    def store_all(self, pcm: np.ndarray, last_cut: str = "shutdown") -> list[dict]:
        return [self.store(n, piece, reason) for n, piece, reason in self.plan(pcm, last_cut)]


@pytest.fixture
def conn(cfg: Config):
    c = dbmod.connect(cfg.db_path)
    yield c
    c.close()
