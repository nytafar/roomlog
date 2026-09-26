"""The worker loop: watchdog notifications reach $NOTIFY_SOCKET and the loop stops cleanly."""

from __future__ import annotations

import socket
import threading
import time

from roomlog_server import sdnotify
from roomlog_server.backends import Router
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.worker import Worker

from test_worker import T0, Fixture


def test_sdnotify_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False


def test_run_forever_feeds_watchdog_and_stops(tmp_path, monkeypatch):
    sock_path = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(sock_path))
    srv.settimeout(5)
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))

    fx = Fixture(tmp_path, poll_s=0.01)
    a = fx.add(T0, 3)
    stop = threading.Event()
    w = fx.worker(FakeBackend())
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and fx.chunk(a)["status"] != "done":
        time.sleep(0.02)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert fx.chunk(a)["status"] == "done"

    msgs = []
    srv.settimeout(0.2)
    try:
        while True:
            msgs.append(srv.recv(64).decode())
    except socket.timeout:
        pass
    srv.close()
    assert msgs[0] == "READY=1"
    assert "WATCHDOG=1" in msgs
