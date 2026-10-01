"""Regression tests: orphan scan, big sessions, calendar-valid timestamps, temp-file races, short fuzzy terms, 411 and capped reads (review items 2, 3, 4, 6, 8, 9, 10)."""

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
from roomlog_server.archive import archive_relpath, scan_orphans
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


# ---------------------------------------------------------------- 2. orphan scan


def test_orphan_scan_skips_bad_sidecar_and_uses_one_query(tmp_path):
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    day = cfg.archive_dir / "2026/09/26"
    day.mkdir(parents=True)
    good = b"good" * 50
    meta = make_sidecar(good)
    good_path = cfg.archive_dir / archive_relpath(1790417732417, meta["sha256"])
    good_path.write_bytes(good)
    good_path.with_suffix(".json").write_text(meta_header(meta))
    (day / "20260926T101533000Z_bbbbbbbb.opus").write_bytes(b"bad")
    (day / "20260926T101533000Z_bbbbbbbb.json").write_text("{not json")
    (day / "20260926T101534000Z_cccccccc.opus").write_bytes(b"bad2")
    (day / "20260926T101534000Z_cccccccc.json").write_text(json.dumps({"schema_version": 1}))
    queries: list[str] = []
    conn.set_trace_callback(queries.append)
    recovered = scan_orphans(conn, cfg.archive_dir)
    conn.set_trace_callback(None)
    assert recovered == [str(good_path.relative_to(cfg.archive_dir))]
    assert sum("WHERE path" in q for q in queries) == 0
    assert sum(q.startswith("SELECT path FROM chunks") for q in queries) == 1
    # ingest still starts with the bad sidecars in place
    srv = make_server(cfg, tokens=TOKENS)
    srv.server_close()
    # the index exists and the schema is current
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "chunks_path" in idx
    assert dbmod.user_version(conn) == dbmod.SCHEMA_VERSION
    conn.close()


def test_corrupt_orphan_is_skipped_and_good_retry_is_stored(tmp_path):
    cfg = make_config(tmp_path)
    good = b"good audio" * 30
    meta = make_sidecar(good)
    rel = archive_relpath(1790417732417, meta["sha256"])
    opus = cfg.archive_dir / rel
    opus.parent.mkdir(parents=True)
    opus.write_bytes(b"corrupt audio")
    opus.with_suffix(".json").write_text(meta_header(meta))
    srv = make_server(cfg, tokens=TOKENS)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = dbmod.connect(cfg.db_path)
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        conn.close()
        client = Client(*srv.server_address[:2])
        status, payload = client.put_chunk(good, meta)
        assert status == 200  # existing files were repaired during recovery
        assert payload["path"] == rel
        assert opus.read_bytes() == good
    finally:
        srv.shutdown()
        srv.server_close()


def test_migration_from_v1_adds_index(tmp_path):
    import sqlite3
    cfg = make_config(tmp_path)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg.db_path), isolation_level=None)
    dbmod.migrate(conn, target=1)
    assert dbmod.user_version(conn) == 1
    conn.close()
    conn = dbmod.connect(cfg.db_path)
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "chunks_path" in idx and dbmod.user_version(conn) == dbmod.SCHEMA_VERSION
    conn.close()


# ---------------------------------------------------------------- 3. big sessions


def test_session_with_more_chunks_than_sqlite_variables(conn):
    n = 33_000
    rows = []
    for i in range(n):
        start = T0 + i * 1000
        rows.append((f"{i:064x}", "oma", start, start + 900, 900, f"p/{i}.opus", "{}", "r", 0, 0, 1, start))
    conn.executemany(
        """INSERT INTO chunks (sha256, device_id, start_utc_ms, end_utc_ms, duration_ms, path, meta_json,
           run_id, epoch, discontinuity, clock_synced, received_utc_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) VALUES (1, 'oma', 0, ?, ?, 0, 'x', 'm')", (T0, T0 + 100))
    assert rebuild_sessions(conn, 300, now=T0) == 1
    s = conn.execute("SELECT n_chunks, n_segments FROM sessions").fetchone()
    assert (s["n_chunks"], s["n_segments"]) == (n, 1)
    assert conn.execute("SELECT count(*) FROM chunks WHERE session_id IS NULL").fetchone()[0] == 0


# ---------------------------------------------------------------- 4. calendar-valid start_utc


@pytest.fixture
def server(tmp_path):
    cfg = make_config(tmp_path)
    srv = make_server(cfg, tokens=TOKENS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, srv, Client(*srv.server_address[:2])
    srv.shutdown()
    srv.server_close()


def test_impossible_timestamps_are_422(server):
    cfg, srv, client = server
    body = b"t" * 100
    for bad in ("2026-02-30T10:00:00.000Z", "2026-09-26T24:00:00.000Z", "2026-13-01T00:00:00.000Z"):
        with pytest.raises(SidecarError):
            validate_sidecar(make_sidecar(body, start_utc=bad))
        status, payload = client.put_chunk(body, make_sidecar(body, start_utc=bad))
        assert status == 422, bad
        assert "start_utc" in payload["error"]


# ---------------------------------------------------------------- 6. concurrent PUTs of one sha


def test_concurrent_same_sha_uploads_never_500(server):
    cfg, srv, client = server
    body = b"same" * 500
    meta = make_sidecar(body)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            status, payload = client.put_chunk(body, meta)
            with lock:
                results.append(status)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 80
    assert set(results) <= {200, 201}, sorted(set(results))
    # A racing duplicate may find the first request's files before its row and take the
    # file-without-row branch (200 by design), so 201 is at most once, never required.
    assert results.count(201) <= 1
    conn = dbmod.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    conn.close()
    assert not list(cfg.archive_dir.rglob("*.tmp"))
    opus = next(cfg.archive_dir.rglob("*.opus"))
    assert opus.read_bytes() == body
    assert opus.with_suffix(".json").read_bytes() == meta_header(meta).encode()


def test_concurrent_revised_sidecars_choose_one_path_and_metadata(server):
    cfg, srv, client = server
    body = b"same audio, revised clock" * 80
    for i in range(6):
        metas = [make_sidecar(body, start_utc=f"2026-09-26T10:{i:02d}:00.000Z"),
                 make_sidecar(body, start_utc=f"2026-09-26T11:{i:02d}:00.000Z")]
        barrier = threading.Barrier(3)
        replies = []

        def upload(meta):
            barrier.wait()
            replies.append(client.put_chunk(body, meta))

        workers = [threading.Thread(target=upload, args=(meta,)) for meta in metas]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()
        assert sorted(status for status, _ in replies) == [200, 201] if i == 0 else [200, 200]
        paths = {payload["path"] for _, payload in replies}
        assert len(paths) == 1
        (relpath,) = paths
        conn = dbmod.connect(cfg.db_path)
        row = conn.execute("SELECT path, meta_json FROM chunks WHERE sha256=?", (metas[0]["sha256"],)).fetchone()
        assert row["path"] == relpath
        assert json.loads((cfg.archive_dir / relpath).with_suffix(".json").read_bytes()) == json.loads(row["meta_json"])
        conn.close()
        assert [str(p.relative_to(cfg.archive_dir)) for p in cfg.archive_dir.rglob("*.opus")] == [relpath]


# ---------------------------------------------------------------- 8. short fuzzy terms


def test_fuzzy_short_terms_still_constrain(conn):
    from test_db import insert_segment, seed_chunk
    cid = seed_chunk(conn)
    insert_segment(conn, cid, 0, "Det er OK, vi må gå til butikken")
    insert_segment(conn, cid, 1, "Butikken er stengt")
    insert_segment(conn, cid, 2, "Ingen av delene")
    texts = lambda q: [r["text"] for r in search(conn, q, fuzzy=True)]
    assert texts("ok") == ["Det er OK, vi må gå til butikken"]
    assert texts("må gå") == ["Det er OK, vi må gå til butikken"]
    assert texts("MÅ") == ["Det er OK, vi må gå til butikken"]
    assert texts("butik zz") == []
    assert sorted(texts("butik")) == ["Butikken er stengt", "Det er OK, vi må gå til butikken"]
    assert texts("butik ok") == ["Det er OK, vi må gå til butikken"]
    assert texts("stengt er") == ["Butikken er stengt"]
    assert texts("zz") == []


# ---------------------------------------------------------------- 9 + 10. 411 and capped reads


def raw_request(host, port, request: bytes, send_body: bytes = b"") -> tuple[int, dict]:
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(request)
    if send_body:
        try:
            s.sendall(send_body)
        except OSError:
            pass
    data = b""
    try:
        while True:
            part = s.recv(65536)
            if not part:
                break
            data += part
            if b"\r\n\r\n" in data:
                head, _, rest = data.partition(b"\r\n\r\n")
                m = re.search(rb"Content-Length: (\d+)", head)
                if m and len(rest) >= int(m.group(1)):
                    break
    except socket.timeout:
        pass
    s.close()
    status = int(data.split(b" ", 2)[1])
    body = data.partition(b"\r\n\r\n")[2]
    return status, json.loads(body) if body else {}


def test_put_without_content_length_or_chunked_is_411(server):
    cfg, srv, client = server
    host, port = srv.server_address[:2]
    body = b"n" * 100
    meta = make_sidecar(body)
    sha = hashlib.sha256(body).hexdigest()
    head = (f"PUT /v1/chunks/{sha} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer tok-oma\r\n"
            f"X-Roomlog-Meta: {meta_header(meta)}\r\nContent-Type: audio/ogg\r\n")
    status, _ = raw_request(host, port, (head + "\r\n").encode())
    assert status == 411
    chunked = head + "Transfer-Encoding: chunked\r\n\r\n64\r\n" + body.decode() + "\r\n0\r\n\r\n"
    status, _ = raw_request(host, port, chunked.encode())
    assert status == 411
    assert not list(cfg.archive_dir.rglob("*.opus"))


def test_401_and_404_do_not_read_absurd_bodies(server):
    cfg, srv, client = server
    huge = 100 * 1024 * 1024
    for token, path in (("nope", "/v1/chunks/" + "a" * 64), ("tok-oma", "/v1/nothing")):
        status, _ = client.request("PUT", path, body=b"x" * 10,
                                   headers={"Authorization": f"Bearer {token}"}, content_length=huge)
        assert status in (401, 404)
    # An ordinary rejected upload still receives an explicit 401.
    status, _ = client.put_chunk(b"y" * 1000, make_sidecar(b"y" * 1000), token="nope")
    assert status == 401


def test_rejected_put_does_not_wait_for_an_incomplete_body(server):
    _, srv, _ = server
    host, port = srv.server_address[:2]
    sha = "a" * 64
    request = (f"PUT /v1/chunks/{sha} HTTP/1.1\r\nHost: x\r\n"
               "Authorization: Bearer invalid\r\nContent-Length: 1048576\r\n\r\n")
    status, _ = raw_request(host, port, request.encode())
    assert status == 401
