"""WAV → capture → Opus spool → HTTP upload → ingest → worker → CLI search."""

from __future__ import annotations

import math
import shutil
import threading
import wave

import numpy as np
import pytest

from roomlog_edge.capture import Pipeline
from roomlog_edge.config import Config as EdgeConfig
from roomlog_edge.encode import Encoder
from roomlog_edge.spool import Spool
from roomlog_edge.uploader import Client as EdgeClient, Uploader
from roomlog_edge.vad import FunctionVad
from roomlog_server import db as dbmod
from roomlog_server.backends import Router
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.cli import main as roomlog_cli
from roomlog_server.config import BackendConfig, Config as ServerConfig
from roomlog_server.ingest import make_server
from roomlog_server.queries import search
from roomlog_server.verify import verify_archive
from roomlog_server.worker import Worker

RATE = 16000
WINDOW = 512
NS = 1_000_000_000
LATENCY_NS = 40_000_000


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is the workstation encoder")
def test_wav_capture_reaches_search(tmp_path, capsys):
    token = "e2e-device-token"
    device_id = "oma"
    edge_state = tmp_path / "edge"
    edge_state.mkdir()
    token_file = edge_state / "token"
    token_file.write_text(token)
    edge_cfg = EdgeConfig(
        device_id=device_id, server_url="http://127.0.0.1:0", token_file=token_file,
        spool_dir=edge_state / "spool", model_path=edge_state / "unused.onnx",
        status_dir=edge_state / "status", metrics_file=edge_state / "metrics.prom",
    )
    server_cfg = ServerConfig(
        config_dir=tmp_path / "server-config", data_dir=tmp_path / "server-data",
        bind="127.0.0.1:0", backends=[BackendConfig(name="fake", type="fake", model="fake-model")],
    )

    # Real WAV input, with VAD deliberately replaced by an energy gate so no
    # microphone or model weights are needed. Durations align to 512-frame blocks.
    silence_before = np.zeros(32 * WINDOW, dtype=np.int16)
    n = np.arange(90 * WINDOW)
    speech = (12000 * np.sin(2 * math.pi * 440 * n / RATE)).astype(np.int16)
    silence_after = np.zeros(65 * WINDOW, dtype=np.int16)
    wav_path = tmp_path / "source.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.concatenate([silence_before, speech, silence_after]).tobytes())

    spool = Spool(edge_cfg.spool_dir)
    vad = FunctionVad(lambda samples: 0.95 if np.sqrt(np.mean(samples * samples)) > 0.05 else 0.02)
    pipeline = Pipeline(edge_cfg, vad, Encoder("ffmpeg"), spool, clock_synced=True)
    mono_zero = 5_000 * NS
    offset = 1_790_000_000 * NS - mono_zero
    with wave.open(str(wav_path), "rb") as wav:
        for i in range(wav.getnframes() // WINDOW):
            pcm = wav.readframes(WINDOW)
            mono = mono_zero + (i + 1) * WINDOW * NS // RATE + LATENCY_NS
            pipeline.process_block(pcm, WINDOW, mono, mono + offset,
                                   WINDOW * NS // RATE + LATENCY_NS)
    pipeline.shutdown()
    pending = spool.entries("pending")
    assert len(pending) == 1
    assert pending[0].opus.read_bytes().startswith(b"OggS")

    server = make_server(server_cfg, tokens={token: device_id})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        client = EdgeClient(f"http://{host}:{port}", token)
        outcomes = Uploader(spool, client).run_once()
        assert len(outcomes) == 1 and outcomes[0].action == "ack"
        assert spool.entries("pending") == []

        conn = dbmod.connect(server_cfg.db_path)
        try:
            worker = Worker(server_cfg, conn, Router([FakeBackend()]))
            result = worker.run_once()
            assert (result.claimed, result.done, result.failed) == (1, 1, 0)
            rows = search(conn, "ord00")
            assert len(rows) == 1 and rows[0]["device_id"] == device_id
            assert verify_archive(conn, server_cfg.archive_dir)["ok"]
        finally:
            conn.close()

        config_file = tmp_path / "server.toml"
        config_file.write_text(f'[paths]\ndata_dir = "{server_cfg.data_dir}"\n')
        assert roomlog_cli(["--config", str(config_file), "search", "ord00"]) == 0
        assert "ord00" in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
