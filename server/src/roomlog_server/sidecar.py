"""Sidecar validation, hand-written to mirror `contract/sidecar.schema.json` (schema_version 1).

Additive fields never bump the version: unknown keys are accepted and stored verbatim.
"""

from __future__ import annotations

import re
from typing import Any

from .times import iso_to_ms

REQUIRED = (
    "schema_version",
    "device_id",
    "sha256",
    "start_utc",
    "duration_s",
    "run_id",
    "epoch",
    "discontinuity",
    "clock_synced",
)

_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_START_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CUT_REASONS = {"silence", "cap", "discontinuity", "shutdown"}
KINDS = {"speech", "raw"}
RAW_SEGMENT_SAMPLES = 480_000  # 30.0 s at 16 kHz, the raw segment length (ADR 0005)
SAMPLE_RATE = 16000


def kind_of(meta: dict[str, Any]) -> str:
    """`speech` when absent: everything the Linux edge sends today."""
    return meta.get("kind", "speech")


class SidecarError(ValueError):
    pass


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float))) and not isinstance(v, bool)


def validate_sidecar(meta: Any) -> dict[str, Any]:
    """Raise `SidecarError` with a message naming the first offending field."""
    if not isinstance(meta, dict):
        raise SidecarError("sidecar must be a JSON object")
    for key in REQUIRED:
        if key not in meta:
            raise SidecarError(f"missing required field {key!r}")
    if meta["schema_version"] != 1 or not _is_int(meta["schema_version"]):
        raise SidecarError("schema_version must be 1")
    if not isinstance(meta["device_id"], str) or not _DEVICE_ID.match(meta["device_id"]):
        raise SidecarError("device_id must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if not isinstance(meta["sha256"], str) or not _SHA256.match(meta["sha256"]):
        raise SidecarError("sha256 must be 64 lowercase hex characters")
    if not isinstance(meta["start_utc"], str) or not _START_UTC.match(meta["start_utc"]):
        raise SidecarError("start_utc must be ISO 8601 with millisecond precision and Z")
    try:
        iso_to_ms(meta["start_utc"])  # the regex accepts 2026-02-30 and 24:00; the calendar does not
    except ValueError:
        raise SidecarError("start_utc is not a valid calendar time") from None
    d = meta["duration_s"]
    if not _is_num(d) or not (0 < d <= 31):
        raise SidecarError("duration_s must be a number in (0, 31]")
    if "sample_rate" in meta and meta["sample_rate"] != 16000:
        raise SidecarError("sample_rate must be 16000")
    if not isinstance(meta["run_id"], str) or not _UUID.match(meta["run_id"]):
        raise SidecarError("run_id must be a UUID")
    if not _is_int(meta["epoch"]) or meta["epoch"] < 0:
        raise SidecarError("epoch must be a non-negative integer")
    for key in ("n_start",):
        if key in meta and (not _is_int(meta[key]) or meta[key] < 0):
            raise SidecarError(f"{key} must be a non-negative integer")
    if "n_samples" in meta and (not _is_int(meta["n_samples"]) or meta["n_samples"] < 1):
        raise SidecarError("n_samples must be a positive integer")
    for key in ("discontinuity", "clock_synced"):
        if not isinstance(meta[key], bool):
            raise SidecarError(f"{key} must be a boolean")
    if "clock_step" in meta and not isinstance(meta["clock_step"], bool):
        raise SidecarError("clock_step must be a boolean")
    if "cut_reason" in meta and meta["cut_reason"] not in CUT_REASONS:
        raise SidecarError("cut_reason must be one of silence|cap|discontinuity|shutdown")
    if "vad" in meta:
        vad = meta["vad"]
        if not isinstance(vad, dict):
            raise SidecarError("vad must be an object")
        for key in ("model", "version"):
            if key in vad and not isinstance(vad[key], str):
                raise SidecarError(f"vad.{key} must be a string")
        if "threshold" in vad and not _is_num(vad["threshold"]):
            raise SidecarError("vad.threshold must be a number")
    if "edge_version" in meta and not isinstance(meta["edge_version"], str):
        raise SidecarError("edge_version must be a string")
    if "session_hint" in meta and meta["session_hint"] is not None and not isinstance(meta["session_hint"], str):
        raise SidecarError("session_hint must be a string or null")
    if "multi_speaker" in meta and meta["multi_speaker"] is not None and not isinstance(meta["multi_speaker"], bool):
        raise SidecarError("multi_speaker must be a boolean or null")
    if "kind" in meta and meta["kind"] not in KINDS:
        raise SidecarError("kind must be speech or raw")
    if kind_of(meta) == "raw":
        _validate_raw(meta)
    return meta


def _validate_raw(meta: dict[str, Any]) -> None:
    """The raw rules of CONTRACT.md: sample-counter cut, no VAD, 480 000 samples unless last."""
    for key in ("n_start", "n_samples", "cut_reason"):
        if key not in meta:
            raise SidecarError(f"raw segment requires {key}")
    if "vad" in meta:
        raise SidecarError("raw segment must not carry a vad object")
    n = meta["n_samples"]
    reason = meta["cut_reason"]
    if n > RAW_SEGMENT_SAMPLES:
        raise SidecarError(f"raw segment n_samples must be at most {RAW_SEGMENT_SAMPLES}")
    if n == RAW_SEGMENT_SAMPLES:
        if reason != "cap":
            raise SidecarError("a full raw segment must have cut_reason cap")
    elif reason not in ("discontinuity", "shutdown"):
        raise SidecarError("a short raw segment must have cut_reason discontinuity or shutdown")
    if abs(float(meta["duration_s"]) - n / SAMPLE_RATE) > 0.00101:
        raise SidecarError("raw segment duration_s must equal n_samples / 16000")
