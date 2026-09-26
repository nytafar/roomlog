"""Sidecar JSON (contract §2.2) and a light validator mirroring the schema.

The validator covers the required fields, types and patterns of
``contract/sidecar.schema.json`` without a jsonschema dependency; the test
suite checks it against the contract examples.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1
CUT_REASONS = ("silence", "cap", "discontinuity", "shutdown")
REQUIRED = ("schema_version", "device_id", "sha256", "start_utc", "duration_s",
            "run_id", "epoch", "discontinuity", "clock_synced")

_DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def format_utc(utc_ns: int) -> str:
    """ISO 8601 with millisecond precision and a Z suffix."""
    ms = (utc_ns + 500_000) // 1_000_000
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def compact_utc(start_utc: str) -> str:
    """``2026-09-26T10:15:32.417Z`` → ``20260926T101532417Z``."""
    return re.sub(r"[-:.]", "", start_utc)


def build(*, device_id: str, sha256: str, utc_ns: int, n_start: int, n_samples: int,
          run_id: str, epoch: int, discontinuity: bool, clock_step: bool, clock_synced: bool,
          cut_reason: str, vad: dict, edge_version: str, sample_rate: int = 16000) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "device_id": device_id,
        "sha256": sha256,
        "start_utc": format_utc(utc_ns),
        "duration_s": round(n_samples / sample_rate, 3),
        "sample_rate": sample_rate,
        "run_id": run_id,
        "epoch": epoch,
        "n_start": n_start,
        "n_samples": n_samples,
        "discontinuity": discontinuity,
        "clock_step": clock_step,
        "clock_synced": clock_synced,
        "cut_reason": cut_reason,
        "vad": vad,
        "edge_version": edge_version,
        "session_hint": None,
        "multi_speaker": None,
    }


def dumps_wire(meta: dict) -> str:
    """The ``X-Roomlog-Meta`` header value: one line, ASCII only."""
    return json.dumps(meta, ensure_ascii=True, separators=(",", ":"))


def dumps_file(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=True, indent=2) + "\n"


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool))


def validate(meta: dict) -> list[str]:
    """Return a list of problems; empty means valid."""
    errs: list[str] = []
    if not isinstance(meta, dict):
        return ["sidecar is not an object"]
    for k in REQUIRED:
        if k not in meta:
            errs.append(f"missing {k}")
    if errs:
        return errs
    if meta["schema_version"] != 1 or not _is_int(meta["schema_version"]):
        errs.append("schema_version must be 1")
    if not isinstance(meta["device_id"], str) or not _DEVICE_RE.match(meta["device_id"]):
        errs.append("device_id pattern")
    if not isinstance(meta["sha256"], str) or not _SHA_RE.match(meta["sha256"]):
        errs.append("sha256 pattern")
    if not isinstance(meta["start_utc"], str) or not _UTC_RE.match(meta["start_utc"]):
        errs.append("start_utc pattern")
    if not _is_num(meta["duration_s"]) or not (0 < meta["duration_s"] <= 31):
        errs.append("duration_s range")
    if not isinstance(meta["run_id"], str) or not _UUID_RE.match(meta["run_id"]):
        errs.append("run_id uuid")
    if not _is_int(meta["epoch"]) or meta["epoch"] < 0:
        errs.append("epoch")
    for k in ("discontinuity", "clock_synced"):
        if not _is_bool(meta[k]):
            errs.append(f"{k} must be boolean")
    if "clock_step" in meta and not _is_bool(meta["clock_step"]):
        errs.append("clock_step must be boolean")
    if "sample_rate" in meta and meta["sample_rate"] != 16000:
        errs.append("sample_rate")
    if "n_start" in meta and (not _is_int(meta["n_start"]) or meta["n_start"] < 0):
        errs.append("n_start")
    if "n_samples" in meta and (not _is_int(meta["n_samples"]) or meta["n_samples"] < 1):
        errs.append("n_samples")
    if "cut_reason" in meta and meta["cut_reason"] not in CUT_REASONS:
        errs.append("cut_reason")
    if "vad" in meta and not isinstance(meta["vad"], dict):
        errs.append("vad must be an object")
    if "edge_version" in meta and not isinstance(meta["edge_version"], str):
        errs.append("edge_version")
    if "session_hint" in meta and not (meta["session_hint"] is None or isinstance(meta["session_hint"], str)):
        errs.append("session_hint")
    if "multi_speaker" in meta and not (meta["multi_speaker"] is None or _is_bool(meta["multi_speaker"])):
        errs.append("multi_speaker")
    return errs
