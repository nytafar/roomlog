"""TOML configuration for the edge (design §3.1, §6).

Every path comes from the file; ``~`` and ``$VAR`` are expanded. Nothing here
assumes /etc or /var.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_ENV = "ROOMLOG_EDGE_CONFIG"


class ConfigError(ValueError):
    pass


def _path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


@dataclass
class AudioConfig:
    device: str | None = None  # None → PortAudio default; e.g. "plughw:CARD=USB"
    sample_rate: int = 16000
    blocksize: int = 512
    zero_exit_s: float = 60.0  # exactly-zero samples this long → exit 3


@dataclass
class VadConfig:
    threshold: float = 0.5
    neg_threshold: float = 0.35
    model_version: str = "v6.2.3"


@dataclass
class TimelineConfig:
    lateness_limit_ms: int = 200  # silent-loss detector: a block this late counts
    late_blocks: int = 3  # ... and this many in a row open a new epoch
    step_limit_ms: int = 50  # real-minus-monotonic offset change that counts as a clock step
    reref_interval_s: float = 60.0  # drift: new anchor at the least-late block per interval


@dataclass
class ChunkerConfig:
    pad_ms: int = 300
    min_silence_ms: int = 1500
    min_speech_ms: int = 250
    max_chunk_s: float = 30.0
    min_pause_ms: int = 100
    ring_s: float = 60.0


@dataclass
class EncoderConfig:
    backend: str = "auto"  # auto | opusenc | ffmpeg
    bitrate_kbps: int = 24


@dataclass
class SpoolConfig:
    max_bytes: int = 2 * 1024**3
    min_free_fraction: float = 0.05


@dataclass
class UploaderConfig:
    timeout_s: float = 30.0
    max_backoff_s: float = 300.0
    idle_poll_s: float = 5.0


@dataclass
class ClockConfig:
    sync_wait_s: float = 120.0
    sync_poll_s: float = 10.0


@dataclass
class HealthConfig:
    healthchecks_url: str | None = None
    frame_age_max_s: float = 60.0
    upload_stall_s: float = 3600.0
    status_stale_s: float = 30.0


@dataclass
class Config:
    device_id: str
    server_url: str
    token_file: Path
    spool_dir: Path
    model_path: Path
    status_dir: Path
    metrics_file: Path
    model_sha256: str | None = None
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    encoder: EncoderConfig = field(default_factory=EncoderConfig)
    spool: SpoolConfig = field(default_factory=SpoolConfig)
    uploader: UploaderConfig = field(default_factory=UploaderConfig)
    clock: ClockConfig = field(default_factory=ClockConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def capture_status(self) -> Path:
        return self.status_dir / "capture.json"

    @property
    def uploader_status(self) -> Path:
        return self.status_dir / "uploader.json"

    def read_token(self) -> str:
        try:
            return self.token_file.read_text().strip()
        except OSError as e:
            raise ConfigError(f"cannot read token file {self.token_file}: {e}") from e


_SECTIONS = {
    "audio": AudioConfig,
    "vad": VadConfig,
    "timeline": TimelineConfig,
    "chunker": ChunkerConfig,
    "encoder": EncoderConfig,
    "spool": SpoolConfig,
    "uploader": UploaderConfig,
    "clock": ClockConfig,
    "health": HealthConfig,
}

_REQUIRED_PATHS = ("token_file", "spool_dir", "model_path", "status_dir", "metrics_file")


def from_dict(data: dict) -> Config:
    missing = [k for k in ("device_id", "server_url", *_REQUIRED_PATHS) if k not in data]
    if missing:
        raise ConfigError(f"missing config keys: {', '.join(missing)}")
    kwargs: dict = {
        "device_id": str(data["device_id"]),
        "server_url": str(data["server_url"]).rstrip("/"),
        "model_sha256": data.get("model_sha256") or None,
    }
    for k in _REQUIRED_PATHS:
        kwargs[k] = _path(str(data[k]))
    for name, cls in _SECTIONS.items():
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(section) - known
        if unknown:
            raise ConfigError(f"unknown keys in [{name}]: {', '.join(sorted(unknown))}")
        kwargs[name] = cls(**section)
    return Config(**kwargs)


def load(path: str | os.PathLike) -> Config:
    p = _path(str(path))
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"bad TOML in {p}: {e}") from e
    return from_dict(data)


def default_config_path() -> Path:
    env = os.environ.get(DEFAULT_CONFIG_ENV)
    if env:
        return _path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return _path(f"{xdg}/roomlog-edge/edge.toml")
