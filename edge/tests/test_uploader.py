"""Uploader against an in-process fake ingest returning every status in §2.3."""

import hashlib
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from roomlog_edge import sidecar
from roomlog_edge.spool import Spool
from roomlog_edge.uploader import ACK, FAIL, RETRY, Client, Uploader, backoff_s, classify

RUN = "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b"


class FakeIngest:
    """Scripted responses: ``script`` is a list of (status, body-or-None) per
    request, in order; the last entry repeats. Records every request."""

    def __init__(self, script, token="secret-token"):
        self.script = list(script)
        self.token = token
        self.requests = []
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _next(self):
                with server.lock:
                    item = server.script.pop(0) if len(server.script) > 1 else server.script[0]
                return item

            def do_PUT(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                meta = self.headers.get("X-Roomlog-Meta")
                server.requests.append({
                    "path": self.path, "auth": self.headers.get("Authorization"),
                    "ctype": self.headers.get("Content-Type"), "meta": meta, "body": body,
                })
                status, resp = self._next()
                if resp is None:
                    if status in (200, 201):
                        resp = {"sha256": hashlib.sha256(body).hexdigest(),
                                "status": "created" if status == 201 else "exists",
                                "path": "2026/09/26/x.opus"}
                    else:
                        resp = {"error": f"status {status}"}
                if status == "drop":
                    self.connection.close()
                    return
                data = json.dumps(resp).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/whoami":
                    ok = self.headers.get("Authorization") == f"Bearer {server.token}"
                    data = json.dumps({"device_id": "oma"} if ok else {"error": "unauthorized"}).encode()
                    self.send_response(200 if ok else 401)
                else:
                    data = b'{"ok": true}'
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ingest():
    servers = []

    def make(script):
        s = FakeIngest(script)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def _spool_with(tmp_path, n=1):
    sp = Spool(tmp_path / "spool")
    entries = []
    for i in range(n):
        meta = sidecar.build(device_id="oma", sha256="0" * 64, utc_ns=1_790_000_000_000_000_000 + i * 10**9,
                             n_start=i * 16000, n_samples=16000, run_id=RUN, epoch=0, discontinuity=False,
                             clock_step=False, clock_synced=True, cut_reason="silence", vad={},
                             edge_version="0.1.0")
        entries.append(sp.write(b"OggS-chunk-%d" % i, meta))
    return sp, entries


def _uploader(sp, server, **kw):
    client = Client(server.url, "secret-token", timeout_s=5)
    return Uploader(sp, client, rng=random.Random(0), **kw)


def test_created_and_exists_ack_and_delete(tmp_path, ingest):
    sp, entries = _spool_with(tmp_path, 2)
    srv = ingest([(201, None), (200, None)])
    up = _uploader(sp, srv)
    outcomes = up.run_once()
    assert [o.action for o in outcomes] == [ACK, ACK]
    assert sp.entries("pending") == []
    req = srv.requests[0]
    assert req["auth"] == "Bearer secret-token" and req["ctype"] == "audio/ogg"
    meta = json.loads(req["meta"])
    assert req["path"] == f"/v1/chunks/{meta['sha256']}"
    assert meta["sha256"] == hashlib.sha256(req["body"]).hexdigest()
    assert "\n" not in req["meta"] and req["meta"].isascii()
    # oldest first
    assert srv.requests[0]["body"] == b"OggS-chunk-0"
    assert up.failures == 0 and up.last_success_ns is not None


@pytest.mark.parametrize("status", [409, 413, 422])
def test_permanent_failures_go_to_failed(tmp_path, ingest, status):
    sp, _ = _spool_with(tmp_path, 1)
    up = _uploader(sp, ingest([(status, None)]))
    (o,) = up.run_once()
    assert o.action == FAIL and o.status == status
    assert sp.entries("pending") == [] and sp.stats().failed_files == 1
    assert up.failures == 0  # not a backoff condition


@pytest.mark.parametrize("status", [401, 403, 411, 500, 503])
def test_retryable_statuses_keep_file_and_back_off(tmp_path, ingest, status):
    sp, _ = _spool_with(tmp_path, 2)
    up = _uploader(sp, ingest([(status, None)]))
    outcomes = up.run_once()
    assert len(outcomes) == 1 and outcomes[0].action == RETRY  # stops at the first retry
    assert len(sp.entries("pending")) == 2
    assert up.failures == 1
    assert 0 <= up.next_delay_s(outcomes) <= 2


def test_connection_error_and_drop_are_retry(tmp_path):
    sp, _ = _spool_with(tmp_path, 1)
    client = Client("http://127.0.0.1:1", "t", timeout_s=2)  # nothing listens on port 1
    up = Uploader(sp, client, rng=random.Random(0))
    (o,) = up.run_once()
    assert o.action == RETRY and o.status is None
    assert len(sp.entries("pending")) == 1


def test_dropped_connection_is_retry(tmp_path, ingest):
    sp, _ = _spool_with(tmp_path, 1)
    up = _uploader(sp, ingest([("drop", None), (201, None)]))
    (o,) = up.run_once()
    assert o.action == RETRY
    assert len(sp.entries("pending")) == 1
    (o,) = up.run_once()
    assert o.action == ACK and up.failures == 0


def test_ack_with_wrong_sha_in_body_keeps_file(tmp_path, ingest):
    sp, _ = _spool_with(tmp_path, 1)
    up = _uploader(sp, ingest([(201, {"sha256": "f" * 64, "status": "created", "path": "x"})]))
    (o,) = up.run_once()
    assert o.action == RETRY
    assert len(sp.entries("pending")) == 1


def test_backoff_bounds_and_reset(tmp_path, ingest):
    rng = random.Random(1)
    for k in range(0, 12):
        for _ in range(20):
            assert 0 <= backoff_s(k, 300, rng) <= min(300, 2 ** k)
    assert backoff_s(40, 300, rng) <= 300
    sp, _ = _spool_with(tmp_path, 1)
    up = _uploader(sp, ingest([(500, None), (500, None), (500, None), (201, None)]))
    for expect in (1, 2, 3):
        outs = up.run_once()
        assert up.failures == expect
        assert up.next_delay_s(outs) <= 2 ** expect
    outs = up.run_once()
    assert outs[0].action == ACK and up.failures == 0 and up.next_delay_s(outs) == 0.0
    assert up.next_delay_s([]) == up.idle_poll_s


def test_status_file_and_run_forever_feeds_watchdog(tmp_path, ingest, monkeypatch):
    import socket

    sp, _ = _spool_with(tmp_path, 1)
    sock_path = tmp_path / "notify.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(sock_path))
    srv.settimeout(5)
    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
    up = _uploader(sp, ingest([(201, None)]), status_path=tmp_path / "run" / "uploader.json", idle_poll_s=0.05)
    stop = threading.Event()
    t = threading.Thread(target=up.run_forever, args=(stop,), daemon=True)
    t.start()
    msgs = []
    while len(msgs) < 3:
        msgs.append(srv.recv(64).decode())
    stop.set()
    t.join(5)
    assert msgs[0] == "READY=1" and "WATCHDOG=1" in msgs
    st = json.loads((tmp_path / "run" / "uploader.json").read_text())
    assert st["pending_files"] == 0 and st["uploaded_total"] == 1 and st["last_error"] is None
    srv.close()


def test_whoami(ingest):
    srv = ingest([])
    status, body = Client(srv.url, "secret-token").whoami()
    assert status == 200 and json.loads(body)["device_id"] == "oma"
    status, _ = Client(srv.url, "wrong").whoami()
    assert status == 401


def test_classify_unknown_status_is_retry():
    assert classify(418) == RETRY and classify(302) == RETRY
    assert classify(411) == RETRY  # server: missing Content-Length; never failed/
    assert classify(200) == ACK and classify(201) == ACK
    assert {classify(s) for s in (409, 413, 422)} == {FAIL}
