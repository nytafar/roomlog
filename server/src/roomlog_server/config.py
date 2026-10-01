"""Server configuration: `~/.config/roomlog/server.toml` plus `tokens.toml`.

Every path the server touches comes from here. Defaults follow the design (§4.1):
config under `~/.config/roomlog/`, data under `~/.local/share/roomlog/`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_DIR = Path("~/.config/roomlog")
DEFAULT_DATA_DIR = Path("~/.local/share/roomlog")
DEFAULT_BLOCKLIST = [
    "Teksting av",
    "Takk for at du så på",
    "Thank you for watching",
]


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


@dataclass
class BackendConfig:
    name: str
    type: str  # local | openai | whisper_cpp | fake
    model: str = ""
    base_url: str = ""
    api_key_file: str | None = None
    extra_fields: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 120.0
    max_concurrent: int = 1
    compute_type: str = "int8"
    device: str = "cpu"
    beam_size: int = 5
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Filters:
    min_avg_logprob: float | None = -1.0
    max_compression_ratio: float | None = 2.4
    max_no_speech_prob: float | None = 0.6
    blocklist: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKLIST))


@dataclass
class Config:
    config_dir: Path = field(default_factory=lambda: _expand(DEFAULT_CONFIG_DIR))
    data_dir: Path = field(default_factory=lambda: _expand(DEFAULT_DATA_DIR))
    archive_dir: Path | None = None
    db_path: Path | None = None
    models_dir: Path | None = None
    tokens_file: Path | None = None
    bind: str = "127.0.0.1:8480"
    max_body_bytes: int = 8 * 1024 * 1024
    poll_s: float = 10.0
    batch_size: int = 50
    window_s: float = 30.0
    window_gap_s: float = 0.3
    max_attempts: int = 3
    language: str | None = "no"
    # ADR 0008: a chunk waits this long after its end before STT, so a dictation span for
    # the same audio (posted when the dictation finishes) usually arrives first
    dictation_hold_s: float = 20.0
    session_gap_s: float = 300.0
    filters: Filters = field(default_factory=Filters)
    backends: list[BackendConfig] = field(default_factory=list)
    prom_file: Path | None = None
    healthchecks_url: str | None = None
    # [segmenter]: raw segments → speech chunks on the server (ADR 0005)
    raw_idle_s: float = 120.0
    vad_backend: str = "silero"  # silero | energy (an RMS gate: tests, or runs without the model)
    vad_threshold: float = 0.5
    vad_neg_threshold: float = 0.35
    vad_model_path: Path | None = None

    def __post_init__(self) -> None:
        self.config_dir = _expand(self.config_dir)
        self.data_dir = _expand(self.data_dir)
        self.archive_dir = _expand(self.archive_dir or self.data_dir / "archive")
        self.db_path = _expand(self.db_path or self.data_dir / "roomlog.db")
        self.models_dir = _expand(self.models_dir or self.data_dir / "models")
        self.tokens_file = _expand(self.tokens_file or self.config_dir / "tokens.toml")
        self.prom_file = _expand(self.prom_file or self.data_dir / "roomlog.prom")
        self.vad_model_path = _expand(self.vad_model_path or self.models_dir / "silero_vad.onnx")
        if self.vad_backend not in ("silero", "energy"):
            raise ValueError(f"segmenter.vad must be silero or energy, not {self.vad_backend!r}")
        if not self.backends:
            self.backends = [
                BackendConfig(name="local", type="local", model="NbAiLab/nb-whisper-medium")
            ]

    def local_model_dir(self, repo_id: str) -> Path:
        """Where `fetch-model` puts a HF repo: `<models_dir>/<owner>--<name>/ct2`."""
        assert self.models_dir is not None
        return self.models_dir / repo_id.replace("/", "--")


def _filters_from(d: dict[str, Any]) -> Filters:
    f = Filters()
    if "min_avg_logprob" in d:
        f.min_avg_logprob = d["min_avg_logprob"]
    if "max_compression_ratio" in d:
        f.max_compression_ratio = d["max_compression_ratio"]
    if "max_no_speech_prob" in d:
        f.max_no_speech_prob = d["max_no_speech_prob"]
    if "blocklist" in d:
        f.blocklist = list(d["blocklist"])
    return f


def _backend_from(d: dict[str, Any]) -> BackendConfig:
    known = {f for f in BackendConfig.__dataclass_fields__}
    kwargs = {k: v for k, v in d.items() if k in known}
    if "extra_fields" in kwargs:
        kwargs["extra_fields"] = {k: str(v) for k, v in kwargs["extra_fields"].items()}
    return BackendConfig(**kwargs)


def config_from_dict(raw: dict[str, Any], config_dir: Path | None = None) -> Config:
    paths = raw.get("paths", {})
    ingest = raw.get("ingest", {})
    worker = raw.get("worker", {})
    sessions = raw.get("sessions", {})
    health = raw.get("health", {})
    segmenter = raw.get("segmenter", {})
    kwargs: dict[str, Any] = {}
    if config_dir is not None:
        kwargs["config_dir"] = config_dir
    for key in ("data_dir", "archive_dir", "db_path", "models_dir", "tokens_file"):
        if key in paths:
            kwargs[key] = paths[key]
    if "bind" in ingest:
        kwargs["bind"] = ingest["bind"]
    if "max_body_bytes" in ingest:
        kwargs["max_body_bytes"] = int(ingest["max_body_bytes"])
    for key in ("poll_s", "batch_size", "window_s", "window_gap_s", "max_attempts"):
        if key in worker:
            kwargs[key] = worker[key]
    if "dictation_hold_s" in worker:
        kwargs["dictation_hold_s"] = float(worker["dictation_hold_s"])
    if "language" in worker:
        lang = worker["language"]
        kwargs["language"] = None if lang in ("", "auto", None) else lang
    if "filters" in worker:
        kwargs["filters"] = _filters_from(worker["filters"])
    if "gap_s" in sessions:
        kwargs["session_gap_s"] = float(sessions["gap_s"])
    if "prom_file" in health:
        kwargs["prom_file"] = health["prom_file"]
    if "healthchecks_url" in health:
        kwargs["healthchecks_url"] = health["healthchecks_url"] or None
    if "raw_idle_s" in segmenter:
        kwargs["raw_idle_s"] = float(segmenter["raw_idle_s"])
    if "vad" in segmenter:
        kwargs["vad_backend"] = str(segmenter["vad"])
    if "threshold" in segmenter:
        kwargs["vad_threshold"] = float(segmenter["threshold"])
    if "neg_threshold" in segmenter:
        kwargs["vad_neg_threshold"] = float(segmenter["neg_threshold"])
    if "model_path" in segmenter:
        kwargs["vad_model_path"] = segmenter["model_path"]
    kwargs["backends"] = [_backend_from(b) for b in raw.get("backends", [])]
    cfg = Config(**kwargs)
    # The bind address is pinned per host in service.env (§4.1).
    env_bind = os.environ.get("ROOMLOG_BIND")
    if env_bind:
        cfg.bind = env_bind
    return cfg


def load_config(path: str | Path | None = None) -> Config:
    """Load `server.toml`. Missing file → defaults. `ROOMLOG_CONFIG` overrides the path."""
    if path is None:
        path = os.environ.get("ROOMLOG_CONFIG") or (DEFAULT_CONFIG_DIR / "server.toml")
    p = _expand(path)
    if p.exists():
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    else:
        raw = {}
    return config_from_dict(raw, config_dir=p.parent)


def load_tokens(path: str | Path) -> dict[str, str]:
    """`tokens.toml`: `device_id = "token"`, one line per client. Returns token → device_id."""
    p = _expand(path)
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    out: dict[str, str] = {}
    for device_id, token in raw.items():
        if not isinstance(token, str) or not token:
            raise ValueError(f"tokens.toml: {device_id!r} must map to a non-empty string")
        if token in out:
            raise ValueError("tokens.toml: the same token is used by two devices")
        out[token] = device_id
    return out
