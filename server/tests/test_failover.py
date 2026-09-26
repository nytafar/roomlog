"""Regression tests: backend failover semantics, attempt accounting, requeue, watchdog cadence, per-chunk model attribution (review items 1, 5, 7)."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import threading
import tomllib
from pathlib import Path

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server.archive import scan_orphans
from roomlog_server.backends import BackendError, BackendUnavailable, Router, Segment, Transcript
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.backends.http import post_multipart
from roomlog_server.cli import main
from roomlog_server.ingest import make_server
from roomlog_server.queries import search
from roomlog_server.sessions import rebuild_sessions
from roomlog_server.sidecar import SidecarError, validate_sidecar
from roomlog_server.times import ms_to_iso
from roomlog_server.worker import requeue

from conftest import REPO_ROOT, make_config, make_sidecar, meta_header
from test_backends import FakeState, make_fake_server
from test_ingest import TOKENS, Client
from test_worker import SR, T0, Fixture


# ---------------------------------------------------------------- 1. 4xx failover, attempts, requeue


def test_berget_4xx_fails_over_to_local_without_burning_attempts(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10)
    for exc in (BackendError("HTTP 400", status=400), BackendUnavailable("HTTP 429")):
        fx.conn.execute("UPDATE chunks SET status='pending', attempts=0")
        berget = FakeBackend(name="berget", model_id="nb-large", fail=exc)
        local = FakeBackend(name="local", model_id="nb-medium")
        w = fx.worker(berget, local)
        for _ in range(3):
            w.run_once()
            fx.conn.execute("UPDATE chunks SET status='pending'")
        c = fx.chunk(a)
        assert c["attempts"] == 0, exc
        assert c["model_id"] == "nb-medium"
        assert len(local.calls) == 3


def test_attempt_spent_only_when_every_backend_fails(tmp_path):
    fx = Fixture(tmp_path, max_attempts=3)
    a = fx.add(T0, 10)
    b1 = FakeBackend(name="b1", fail=BackendError("bad request", status=400))
    b2 = FakeBackend(name="b2", fail=BackendUnavailable("503"))
    w = fx.worker(b1, b2)
    for i in range(1, 3):
        assert w.run_once().failed == 1
        c = fx.chunk(a)
        assert (c["status"], c["attempts"]) == ("pending", i)
        assert "b1" in c["error"] and "b2" in c["error"] and "bad request" in c["error"]
    w.run_once()
    assert (fx.chunk(a)["status"], fx.chunk(a)["attempts"]) == ("failed", 3)
    assert w.run_once().claimed == 0
    # requeue puts it back with attempts reset
    assert requeue(fx.conn, fx.cfg.max_attempts) == 1
    c = fx.chunk(a)
    assert (c["status"], c["attempts"], c["error"]) == ("pending", 0, None)


def test_http_429_and_408_are_transient():
    state = FakeState()
    srv = make_fake_server(state)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1/audio/transcriptions"
        for code in (429, 408, 503):
            state.status = code
            with pytest.raises(BackendUnavailable):
                post_multipart(url, {"model": "m"}, b"RIFF", "a.wav", timeout=5)
        state.status = 400
        with pytest.raises(BackendError) as e:
            post_multipart(url, {"model": "m"}, b"RIFF", "a.wav", timeout=5)
        assert e.value.status == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_router_moves_past_backend_error_without_marking_it_down():
    clock = [0.0]
    bad = FakeBackend(name="bad", fail=BackendError("400", status=400))
    good = FakeBackend(name="good")
    r = Router([bad, good], probe_ttl_s=60, clock=lambda: clock[0])
    _, used = r.transcribe(np.ones(160, np.float32), "no")
    assert used is good
    assert r._state["bad"][0] is True  # still healthy: a 400 is per request, not an outage
    down = FakeBackend(name="down", fail=BackendUnavailable("503"))
    r = Router([down, good], probe_ttl_s=60, clock=lambda: clock[0])
    r.transcribe(np.ones(160, np.float32), "no")
    assert r._state["down"][0] is False


def test_requeue_filters(tmp_path):
    fx = Fixture(tmp_path, max_attempts=3)
    a = fx.add(T0, 1, device_id="oma")
    b = fx.add(T0 + 10_000, 1, device_id="pi-work")
    c = fx.add(T0 + 20_000, 1, device_id="oma")
    fx.conn.execute("UPDATE chunks SET status='failed', attempts=3, error='x' WHERE id IN (?, ?)", (a, b))
    fx.conn.execute("UPDATE chunks SET status='pending', attempts=3 WHERE id=?", (c,))  # stranded
    assert requeue(fx.conn, 3, failed_only=True, device_id="oma") == 1
    assert fx.chunk(a)["status"] == "pending" and fx.chunk(a)["attempts"] == 0
    assert fx.chunk(b)["status"] == "failed"
    assert fx.chunk(c)["attempts"] == 3
    assert requeue(fx.conn, 3, since_ms=T0 + 15_000) == 1  # the stranded one, by time
    assert fx.chunk(c)["attempts"] == 0
    assert requeue(fx.conn, 3) == 1  # b
    assert requeue(fx.conn, 3) == 0


def test_cli_requeue(tmp_path, capsys):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 1)
    fx.conn.execute("UPDATE chunks SET status='failed', attempts=3")
    fx.conn.close()
    toml = fx.cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{fx.cfg.data_dir}"\n')
    assert main(["-c", str(toml), "requeue", "--failed", "--device", "oma", "--since", "2026-09-26"]) == 0
    assert capsys.readouterr().out.strip() == "requeued=1"
    assert main(["-c", str(toml), "requeue"]) == 0
    assert capsys.readouterr().out.strip() == "requeued=0"


# ---------------------------------------------------------------- 5. watchdog per window


def test_watchdog_fed_per_window_and_per_backend_call(tmp_path, monkeypatch):
    sock_path = tmp_path / "n.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(sock_path))
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
    fx = Fixture(tmp_path)
    for i in range(4):
        fx.add(T0 + i * 20_000, 12)  # 12 s each: two per window → 2 windows
    res = fx.worker(FakeBackend()).run_once()
    assert res.windows == 2
    srv.settimeout(0.2)
    msgs = []
    try:
        while True:
            msgs.append(srv.recv(64).decode())
    except socket.timeout:
        pass
    srv.close()
    assert msgs.count("WATCHDOG=1") >= res.windows + 2 * res.windows  # per window + before/after each call


# ---------------------------------------------------------------- 7. per-chunk model attribution


def test_per_chunk_rerun_stamps_each_chunk_with_its_backend(tmp_path):
    fx = Fixture(tmp_path)
    a = fx.add(T0, 5)
    b = fx.add(T0 + 10_000, 5)
    c = fx.add(T0 + 20_000, 5)
    wordless = Transcript(segments=[Segment(0, 15.6, " alt")])
    first_single = Transcript(segments=[Segment(0, 5, " a fra berget")])

    calls = {"n": 0}

    def berget_script(audio, language):
        calls["n"] += 1
        if calls["n"] == 1:
            return wordless
        if calls["n"] == 2:
            return first_single
        raise BackendUnavailable("berget went away")

    berget = FakeBackend(name="berget", model_id="nb-large", script=berget_script)
    local = FakeBackend(name="local", model_id="nb-medium")
    res = fx.worker(berget, local).run_once()
    assert res.done == 3
    assert fx.chunk(a)["model_id"] == "nb-large"
    assert fx.segments(a)[0]["model_id"] == "nb-large"
    assert fx.segments(a)[0]["text"] == "a fra berget"
    assert fx.chunk(b)["model_id"] == "nb-medium"
    assert fx.chunk(c)["model_id"] == "nb-medium"
    assert all(s["model_id"] == "nb-medium" for s in fx.segments(b) + fx.segments(c))
    assert len(local.calls) == 2


