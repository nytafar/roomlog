from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from roomlog_server import db as dbmod
from roomlog_server.ingest import make_server

from conftest import make_config, make_sidecar, meta_header

TOKENS = {"tok-oma": "oma", "tok-work": "pi-work"}


class Client:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None, content_length: int | None = None):
        c = http.client.HTTPConnection(self.host, self.port, timeout=10)
        hdrs = dict(headers or {})
        if content_length is not None:
            hdrs["Content-Length"] = str(content_length)
            c.putrequest(method, path)
            for k, v in hdrs.items():
                c.putheader(k, v)
            c.endheaders()
            if body:
                try:
                    c.send(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        else:
            c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            payload = data
        return r.status, payload

    def put_chunk(self, body: bytes, meta: dict | None = None, token: str | None = "tok-oma",
                  sha: str | None = None, meta_raw: str | None = None, **kw):
        sha = sha or hashlib.sha256(body).hexdigest()
        headers = {"Content-Type": "audio/ogg"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if meta_raw is not None:
            headers["X-Roomlog-Meta"] = meta_raw
        elif meta is not None:
            headers["X-Roomlog-Meta"] = meta_header(meta)
        return self.request("PUT", f"/v1/chunks/{sha}", body=body, headers=headers, **kw)


@pytest.fixture
def server(tmp_path: Path):
    cfg = make_config(tmp_path)
    srv = make_server(cfg, tokens=TOKENS)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address[:2]
    yield cfg, srv, Client(host, port)
    srv.shutdown()
    srv.server_close()


def test_healthz_and_whoami(server):
    cfg, srv, client = server
    assert client.request("GET", "/healthz") == (200, {"ok": True})
    assert client.request("GET", "/v1/whoami") == (401, {"error": "missing or unknown token"})
    status, payload = client.request("GET", "/v1/whoami", headers={"Authorization": "Bearer tok-oma"})
    assert (status, payload) == (200, {"device_id": "oma"})
    status, _ = client.request("GET", "/v1/whoami", headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_created_then_exists(server):
    cfg, srv, client = server
    body = b"OggS" + os.urandom(2000)
    meta = make_sidecar(body)
    status, payload = client.put_chunk(body, meta)
    assert status == 201
    assert payload["status"] == "created"
    assert payload["sha256"] == meta["sha256"]
    assert payload["path"] == f"2026/09/26/20260926T101532417Z_{meta['sha256'][:8]}.opus"

    opus = cfg.archive_dir / payload["path"]
    assert opus.read_bytes() == body
    # sidecar stored byte-for-byte as received
    assert opus.with_suffix(".json").read_bytes() == meta_header(meta).encode()
    assert not list(cfg.archive_dir.rglob("*.tmp"))

    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT * FROM chunks WHERE sha256 = ?", (meta["sha256"],)).fetchone()
    assert row["status"] == "pending"
    assert row["device_id"] == "oma"
    assert row["duration_ms"] == 12352
    assert row["end_utc_ms"] - row["start_utc_ms"] == 12352
    assert json.loads(row["meta_json"]) == meta
    conn.close()

    status, payload = client.put_chunk(body, meta)
    assert status == 200
    assert payload["status"] == "exists"
    assert payload["sha256"] == meta["sha256"]


def test_401_missing_or_unknown_token(server):
    cfg, srv, client = server
    body = b"x" * 100
    meta = make_sidecar(body)
    assert client.put_chunk(body, meta, token=None)[0] == 401
    assert client.put_chunk(body, meta, token="wrong")[0] == 401
    assert client.put_chunk(body, meta, token="tok-oma ")[0] == 201  # whitespace tolerated


def test_403_token_device_mismatch(server):
    cfg, srv, client = server
    body = b"x" * 100
    meta = make_sidecar(body, device_id="oma")
    status, payload = client.put_chunk(body, meta, token="tok-work")
    assert status == 403
    assert "error" in payload
    assert not list(cfg.archive_dir.rglob("*.opus"))


def test_409_sha_mismatches(server):
    cfg, srv, client = server
    body = b"y" * 100
    meta = make_sidecar(body)
    other = hashlib.sha256(b"other").hexdigest()
    assert client.put_chunk(body, meta, sha=other)[0] == 409
    meta2 = dict(meta, sha256=other)
    assert client.put_chunk(body, meta2)[0] == 409
    assert not list(cfg.archive_dir.rglob("*.opus"))


def test_413_body_too_large(server):
    cfg, srv, client = server
    big = b"z" * (8 * 1024 * 1024 + 1)
    meta = make_sidecar(big)
    status, payload = client.put_chunk(big, meta)
    assert status == 413
    exact = b"z" * (8 * 1024 * 1024)
    status, _ = client.put_chunk(exact, make_sidecar(exact))
    assert status == 201


def test_413_absurd_declared_length_is_refused_without_reading(server):
    cfg, srv, client = server
    body = b"q" * 10
    status, payload = client.put_chunk(body, make_sidecar(body), content_length=100 * 1024 * 1024)
    assert status == 413


def test_422_sidecar_problems(server):
    cfg, srv, client = server
    body = b"w" * 100
    meta = make_sidecar(body)
    assert client.put_chunk(body, meta=None)[0] == 422  # header missing
    assert client.put_chunk(body, meta_raw="{not json")[0] == 422
    assert client.put_chunk(body, meta_raw="[1,2]")[0] == 422
    for bad in (
        {"schema_version": 2},
        {"device_id": "Oma"},
        {"start_utc": "2026-09-26T10:15:32Z"},
        {"duration_s": 0},
        {"duration_s": 31.5},
        {"epoch": -1},
        {"discontinuity": "no"},
        {"run_id": "not-a-uuid"},
        {"cut_reason": "boredom"},
    ):
        status, payload = client.put_chunk(body, dict(meta, **bad))
        assert status == 422, bad
        assert payload["error"].startswith("sidecar invalid")
    for missing in ("schema_version", "device_id", "sha256", "start_utc", "duration_s",
                    "run_id", "epoch", "discontinuity", "clock_synced"):
        m = dict(meta)
        del m[missing]
        assert client.put_chunk(body, m)[0] == 422, missing


def test_unknown_fields_are_stored(server):
    cfg, srv, client = server
    body = b"u" * 100
    meta = make_sidecar(body, future_field={"a": 1})
    status, payload = client.put_chunk(body, meta)
    assert status == 201
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT meta_json FROM chunks WHERE sha256=?", (meta["sha256"],)).fetchone()
    assert json.loads(row["meta_json"])["future_field"] == {"a": 1}
    conn.close()


def test_404_bad_path(server):
    cfg, srv, client = server
    status, _ = client.request("PUT", "/v1/chunks/abc", body=b"x",
                               headers={"Authorization": "Bearer tok-oma"})
    assert status == 404
    assert client.request("GET", "/nope")[0] == 404


def test_file_without_row_recovery(server):
    cfg, srv, client = server
    body = b"f" * 300
    meta = make_sidecar(body)
    status, payload = client.put_chunk(body, meta)
    assert status == 201
    conn = dbmod.connect(cfg.db_path)
    conn.execute("DELETE FROM chunks WHERE sha256=?", (meta["sha256"],))
    conn.close()
    status, payload2 = client.put_chunk(body, meta)
    assert status == 200
    assert payload2["status"] == "exists"
    assert payload2["path"] == payload["path"]
    conn = dbmod.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM chunks WHERE sha256=?", (meta["sha256"],)).fetchone()[0] == 1
    conn.close()


def test_row_without_file_recovery(server):
    cfg, srv, client = server
    body = b"r" * 300
    meta = make_sidecar(body)
    status, payload = client.put_chunk(body, meta)
    assert status == 201
    opus = cfg.archive_dir / payload["path"]
    opus.unlink()
    opus.with_suffix(".json").unlink()
    status, payload2 = client.put_chunk(body, meta)
    assert status == 200
    assert opus.read_bytes() == body
    assert opus.with_suffix(".json").exists()


def test_row_repair_preserves_received_sidecar_bytes(server):
    cfg, _, client = server
    body = b"verbatim metadata" * 30
    meta = make_sidecar(body)
    raw = json.dumps(dict(reversed(list(meta.items()))), ensure_ascii=True, separators=(", ", ": "))
    status, payload = client.put_chunk(body, meta_raw=raw)
    assert status == 201
    opus = cfg.archive_dir / payload["path"]
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT meta_json FROM chunks WHERE sha256=?", (meta["sha256"],)).fetchone()
    assert row["meta_json"] == raw
    conn.close()
    opus.with_suffix(".json").unlink()
    status, _ = client.put_chunk(body, meta_raw=raw)
    assert status == 200
    assert opus.with_suffix(".json").read_bytes() == raw.encode("ascii")


def test_startup_scan_recovers_orphans(tmp_path: Path):
    cfg = make_config(tmp_path)
    srv = make_server(cfg, tokens=TOKENS)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    client = Client(*srv.server_address[:2])
    body = b"o" * 100
    meta = make_sidecar(body)
    status, payload = client.put_chunk(body, meta)
    assert status == 201
    srv.shutdown()
    srv.server_close()
    conn = dbmod.connect(cfg.db_path)
    conn.execute("DELETE FROM chunks")
    conn.close()
    srv2 = make_server(cfg, tokens=TOKENS)
    srv2.server_close()
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT path, status FROM chunks WHERE sha256=?", (meta["sha256"],)).fetchone()
    assert row["path"] == payload["path"]
    assert row["status"] == "pending"
    conn.close()


def test_concurrent_uploads(server):
    cfg, srv, client = server
    results = []

    def upload(i: int) -> None:
        body = f"chunk-{i}".encode() * 50
        meta = make_sidecar(body, start_utc=f"2026-09-26T10:{i:02d}:00.000Z")
        results.append(client.put_chunk(body, meta)[0])

    threads = [threading.Thread(target=upload, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [201] * 12
    conn = dbmod.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 12
    conn.close()
