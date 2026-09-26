import json

import pytest

from roomlog_edge import config as configmod, sidecar


def test_contract_examples_validate(contract_dir):
    examples = sorted((contract_dir / "examples").glob("*.json"))
    assert len(examples) >= 3
    for p in examples:
        meta = json.loads(p.read_text())
        assert sidecar.validate(meta) == [], p.name


def test_validate_catches_problems():
    good = {
        "schema_version": 1, "device_id": "oma", "sha256": "a" * 64,
        "start_utc": "2026-09-26T10:15:32.417Z", "duration_s": 1.5,
        "run_id": "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b", "epoch": 0,
        "discontinuity": False, "clock_synced": True,
    }
    assert sidecar.validate(good) == []
    assert sidecar.validate({**good, "device_id": "Bad_ID"}) == ["device_id pattern"]
    assert sidecar.validate({**good, "start_utc": "2026-09-26T10:15:32Z"}) == ["start_utc pattern"]
    assert sidecar.validate({**good, "duration_s": 0}) == ["duration_s range"]
    assert sidecar.validate({**good, "cut_reason": "oops"}) == ["cut_reason"]
    assert sidecar.validate({**good, "discontinuity": 1}) == ["discontinuity must be boolean"]
    assert "missing sha256" in sidecar.validate({k: v for k, v in good.items() if k != "sha256"})


def test_format_and_compact_utc():
    ns = 1_790_000_000_123_456_789  # 2026-09-21T...
    s = sidecar.format_utc(ns)
    assert s.endswith("Z") and s[19] == "." and len(s) == 24
    from datetime import datetime, timezone

    expect = datetime.fromtimestamp(1_790_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".123Z"
    assert s == expect
    assert sidecar.compact_utc(s) == expect.replace("-", "").replace(":", "").replace(".", "")
    assert sidecar.format_utc(1_790_000_000_999_600_000).endswith(":21.000Z")  # rounds up


def test_build_and_wire_are_ascii_one_line():
    meta = sidecar.build(device_id="oma", sha256="f" * 64, utc_ns=1_790_000_000_000_000_000,
                         n_start=5, n_samples=16000, run_id="b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b",
                         epoch=2, discontinuity=True, clock_step=False, clock_synced=False,
                         cut_reason="cap", vad={"model": "silero-vad", "version": "v6.2.3", "threshold": 0.5},
                         edge_version="0.1.0")
    assert sidecar.validate(meta) == []
    assert meta["duration_s"] == 1.0 and meta["session_hint"] is None and meta["multi_speaker"] is None
    wire = sidecar.dumps_wire(meta)
    assert "\n" not in wire and wire.isascii() and " " not in wire.split('"start_utc"')[0]


def test_config_loads_with_defaults_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("MYSTATE", str(tmp_path / "state"))
    toml = f'''
device_id = "oma"
server_url = "http://example.invalid:8480/"
token_file = "~/token"
spool_dir = "$MYSTATE/spool"
model_path = "$MYSTATE/silero_vad.onnx"
status_dir = "{tmp_path}/run"
metrics_file = "$MYSTATE/metrics.prom"
[audio]
device = "plughw:CARD=USB"
[chunker]
max_chunk_s = 20.0
'''
    p = tmp_path / "edge.toml"
    p.write_text(toml)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = configmod.load(p)
    assert cfg.server_url == "http://example.invalid:8480"
    assert cfg.token_file == tmp_path / "token"
    assert cfg.spool_dir == tmp_path / "state" / "spool"
    assert cfg.audio.device == "plughw:CARD=USB"
    assert cfg.chunker.max_chunk_s == 20.0 and cfg.chunker.pad_ms == 300
    assert cfg.capture_status == tmp_path / "run" / "capture.json"


def test_config_rejects_missing_and_unknown_keys(tmp_path):
    with pytest.raises(configmod.ConfigError, match="missing config keys: .*spool_dir"):
        configmod.from_dict({"device_id": "x", "server_url": "http://h"})
    from conftest import make_config

    with pytest.raises(configmod.ConfigError, match="unknown keys in \\[audio\\]: devcie"):
        make_config(tmp_path, audio={"devcie": "x"})


def test_example_config_parses(tmp_path, monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "deploy" / "edge" / "edge.toml.example"
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = configmod.load(example)
    assert cfg.device_id and cfg.server_url.startswith("http")
