"""Raw 30 s segments (a thin client) → HTTP upload → ingest → segmenter → worker → CLI search."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import threading
import time

import numpy as np
import pytest

from roomlog_edge.encode import Encoder, roomlog_tags
from roomlog_edge.sidecar import format_utc
from roomlog_edge.uploader import Client as EdgeClient
from roomlog_server import db as dbmod
from roomlog_server.cli import main as roomlog_cli
from roomlog_server.config import BackendConfig, Config as ServerConfig
from roomlog_server.ingest import make_server
from roomlog_server.verify import verify_archive

RATE = 16000
SEGMENT = 480_000
NS = 1_000_000_000
RUN_ID = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"


def raw_sidecar(device_id: str, body: bytes, utc_ns: int, n_start: int, n_samples: int, cut_reason: str) -> dict:
    """What the Android client's `Sidecar.build` produces for a raw segment (plan §3.1)."""
    return {
        "schema_version": 1,
        "kind": "raw",
        "device_id": device_id,
        "sha256": hashlib.sha256(body).hexdigest(),
        "start_utc": format_utc(utc_ns),
        "duration_s": round(n_samples / RATE, 3),
        "sample_rate": RATE,
        "run_id": RUN_ID,
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is the workstation encoder")
def test_raw_segments_reach_search(tmp_path, capsys):
    token = "e2e-phone-token"
    device_id = "s22"
    server_cfg = ServerConfig(
        config_dir=tmp_path / "server-config", data_dir=tmp_path / "server-data",
        bind="127.0.0.1:0", backends=[BackendConfig(name="fake", type="fake", model="fake-model")],
        vad_backend="energy",
    )

    # 25 s silence, 15 s tone across the 30 s seam, 32 s silence: three raw segments, the
    # last one short with a shutdown cut, exactly as the phone would cut them.
    n = np.arange(15 * RATE)
    tone = (12000 * np.sin(2 * math.pi * 440 * n / RATE)).astype(np.int16)
    pcm = np.concatenate([np.zeros(25 * RATE, np.int16), tone, np.zeros(32 * RATE, np.int16)])
    t0_ns = 1_790_000_000 * NS
    encoder = Encoder("ffmpeg")
    uploads: list[tuple[bytes, dict]] = []
    for i, n_start in enumerate(range(0, len(pcm), SEGMENT)):
        piece = pcm[n_start:n_start + SEGMENT]
        reason = "shutdown" if n_start + SEGMENT >= len(pcm) else "cap"
        body = encoder.encode(piece.tobytes(), roomlog_tags(device_id, RUN_ID, 0, n_start, "android-0.1.0"))
        uploads.append((body, raw_sidecar(device_id, body, t0_ns + n_start * NS // RATE, n_start, len(piece), reason)))
    assert [m["n_samples"] for _, m in uploads] == [SEGMENT, SEGMENT, 192_000]

    server = make_server(server_cfg, tokens={token: device_id})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        client = EdgeClient(f"http://{host}:{port}", token)
        # the clock probe the phone uses for clock_synced (ADR 0006)
        before = time.time_ns()
        status, data = client._request("GET", "/v1/time")
        assert status == 200 and before <= json.loads(data)["utc_ns"] <= time.time_ns()
        for body, meta in uploads:
            status, data = client.put_chunk(body, meta)
            assert status == 201, data
            assert json.loads(data)["path"].startswith("raw/")
        status, _ = client.put_chunk(*uploads[0])
        assert status == 200

        config_file = tmp_path / "server.toml"
        config_file.write_text(
            f'[paths]\ndata_dir = "{server_cfg.data_dir}"\n'
            '[segmenter]\nvad = "energy"\n'
            '[[backends]]\nname = "fake"\ntype = "fake"\nmodel = "fake-model"\n'
        )
        base = ["--config", str(config_file)]
        assert roomlog_cli(base + ["worker", "--once"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("claimed=1 done=1 failed=0 windows=1 segmented=1")

        assert roomlog_cli(base + ["search", "ord00"]) == 0
        out = capsys.readouterr().out
        assert "ord00" in out and out.startswith("s22_")  # session id, start, text

        conn = dbmod.connect(server_cfg.db_path, readonly=True)
        try:
            chunk = conn.execute("SELECT * FROM chunks").fetchone()
            assert chunk["kind"] == "derived" and chunk["path"] is None and chunk["status"] == "done"
            assert chunk["n_start"] < SEGMENT < chunk["n_start"] + chunk["n_samples"]
            seg = conn.execute("SELECT * FROM segments").fetchone()
            tone_start_ms = t0_ns // 1_000_000 + 25_000
            assert abs(seg["start_utc_ms"] - tone_start_ms) <= 50
            assert conn.execute("SELECT count(*) FROM raw_segments WHERE status = 'segmented'").fetchone()[0] == 3
            assert verify_archive(conn, server_cfg.archive_dir)["ok"]
        finally:
            conn.close()

        assert roomlog_cli(base + ["status"]) == 0
        out = capsys.readouterr().out
        assert "raw            pending=0 segmented=3 failed=0 derived=1" in out
        assert roomlog_cli(base + ["verify"]) == 0
        assert capsys.readouterr().out.startswith("files=3 rows=3 problems=0 derived=1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
