"""Real Ogg Opus (made with ffmpeg) → ingest → worker with the fake backend → search."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server.audio import decode_opus
from roomlog_server.backends import Router
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.ingest import make_server
from roomlog_server.queries import search
from roomlog_server.worker import Worker

from conftest import make_config, make_sidecar
from test_ingest import TOKENS, Client

ffmpeg = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(ffmpeg is None, reason="ffmpeg not installed")


def make_opus(path, seconds: float, freq: float = 440.0) -> bytes:
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}", "-ar", "16000", "-ac", "1",
         "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_decode_opus_length_and_level(tmp_path):
    p = tmp_path / "t.opus"
    make_opus(p, 1.5)
    a = decode_opus(p)
    assert a.dtype == np.float32
    assert abs(len(a) / 16000 - 1.5) < 0.05
    assert 0.05 < np.abs(a).max() < 1.0


def test_ingest_to_search(tmp_path):
    cfg = make_config(tmp_path)
    srv = make_server(cfg, tokens=TOKENS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = Client(*srv.server_address[:2])
    try:
        for i, (start, dur) in enumerate([("2026-09-26T10:00:00.000Z", 2.0), ("2026-09-26T10:00:05.000Z", 1.0)]):
            body = make_opus(tmp_path / f"c{i}.opus", dur, freq=300 + 100 * i)
            meta = make_sidecar(body, start_utc=start, duration_s=dur)
            status, payload = client.put_chunk(body, meta)
            assert status == 201, payload
    finally:
        srv.shutdown()
        srv.server_close()

    def script(audio, language):
        from roomlog_server.backends import Segment, Transcript, Word
        # one word in each chunk of the window: chunk 0 = [0, 2.0), chunk 1 = [2.3, 3.3)
        return Transcript(segments=[
            Segment(0.2, 0.8, " første", [Word(0.2, 0.8, " første", 0.9)], avg_logprob=-0.1),
            Segment(2.5, 3.0, " andre", [Word(2.5, 3.0, " andre", 0.9)], avg_logprob=-0.1),
        ], language="no")

    conn = dbmod.connect(cfg.db_path)
    backend = FakeBackend(script=script, model_id="fake-e2e")
    res = Worker(cfg, conn, Router([backend], probe_ttl_s=0)).run_once()
    assert (res.claimed, res.done, res.failed, res.windows) == (2, 2, 0, 1)
    joined, _ = backend.calls[0]
    assert abs(len(joined) / 16000 - (2.0 + 0.3 + 1.0)) < 0.1

    hits = search(conn, "andre")
    assert len(hits) == 1
    assert hits[0]["start_utc"] == "2026-09-26T10:00:05.200Z"
    assert hits[0]["session_id"] == "oma_20260926T100000000Z"
    hits = search(conn, "første")
    assert hits[0]["start_utc"] == "2026-09-26T10:00:00.200Z"
    conn.close()
