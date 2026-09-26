"""Ingest HTTP server (§2.3, §4.2): stdlib `ThreadingHTTPServer`.

    PUT /v1/chunks/{sha256}   bearer token, `X-Roomlog-Meta` sidecar, body = .opus bytes
    GET /healthz              no auth
    GET /v1/whoami            bearer token → {"device_id": ...}

Status codes follow CONTRACT.md exactly; the edge's retry/fail decisions depend on them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db as dbmod
from .archive import scan_orphans, store_chunk
from .config import Config, load_tokens
from .sidecar import SidecarError, validate_sidecar

log = logging.getLogger("roomlog.ingest")

_CHUNK_PATH = re.compile(r"^/v1/chunks/([0-9a-f]{64})$")


class IngestApp:
    """Shared state for handler threads: config, tokens and a DB connection per thread."""

    def __init__(self, cfg: Config, tokens: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        assert cfg.archive_dir is not None and cfg.db_path is not None and cfg.tokens_file is not None
        self.archive_dir: Path = cfg.archive_dir
        self.db_path: Path = cfg.db_path
        self.tokens: dict[str, str] = tokens if tokens is not None else load_tokens(cfg.tokens_file)
        self.max_body = cfg.max_body_bytes
        self._local = threading.local()
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        conn = dbmod.connect(self.db_path)
        recovered = scan_orphans(conn, self.archive_dir)
        if recovered:
            log.info("recovered %d archive files without rows", len(recovered))
        conn.close()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = dbmod.connect(self.db_path)
            self._local.conn = c
        return c

    def device_for_token(self, header: str | None) -> str | None:
        """Constant-time lookup: compare against every configured token, no early exit."""
        if not header or not header.startswith("Bearer "):
            return None
        presented = header[len("Bearer "):].strip().encode()
        found: str | None = None
        for token, device_id in self.tokens.items():
            if hmac.compare_digest(presented, token.encode()):
                found = device_id
        return found


class IngestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "roomlog-ingest/1"
    app: IngestApp  # set on the server class

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, drain: int = 0) -> None:
        if drain:
            self._drain(drain)
        self._send(status, {"error": message})

    def _drain(self, n: int) -> None:
        """Read and discard up to n body bytes so the connection can be reused."""
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _content_length(self) -> int | None:
        """Declared body length; None when absent, unparseable or chunked (→ 411)."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return None
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            n = int(raw)
        except ValueError:
            return None
        return n if n >= 0 else None

    def _reject(self, status: int, message: str, length: int | None) -> None:
        """Error reply for requests refused before the body is needed: drain a modest body
        so the client sees the status, close the connection on an absurd one (item: no
        uncapped reads on the 401/404 paths)."""
        if length is None:
            self.close_connection = True
        elif length <= 8 * self.app.max_body:
            self._drain(length)
        else:
            self.close_connection = True
        self._error(status, message)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        if self.path == "/v1/whoami":
            device_id = self.app.device_for_token(self.headers.get("Authorization"))
            if device_id is None:
                self._error(401, "missing or unknown token")
                return
            self._send(200, {"device_id": device_id})
            return
        self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        length = self._content_length()
        m = _CHUNK_PATH.match(self.path)
        if m is None:
            self._reject(404, "not found", length)
            return
        url_sha = m.group(1)

        device_id = self.app.device_for_token(self.headers.get("Authorization"))
        if device_id is None:
            self._reject(401, "missing or unknown token", length)
            return

        if length is None:
            # No usable Content-Length (absent, chunked): the body cannot be trusted, and a
            # 409 here would make the edge discard a good chunk.
            self.close_connection = True
            self._error(411, "Content-Length required")
            return
        if length > self.app.max_body:
            # Drain moderately oversized bodies so the client sees the 413 instead of a
            # broken pipe; refuse to read absurd ones and close instead.
            self._reject(413, f"body larger than {self.app.max_body} bytes", length)
            return

        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self.close_connection = True
            self._error(400, "short body")
            return

        meta_header = self.headers.get("X-Roomlog-Meta")
        if meta_header is None:
            self._error(422, "X-Roomlog-Meta header missing")
            return
        meta_raw = meta_header.encode("ascii", errors="strict") if meta_header.isascii() else None
        if meta_raw is None:
            self._error(422, "X-Roomlog-Meta must be ASCII")
            return
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            self._error(422, "X-Roomlog-Meta is not valid JSON")
            return

        body_sha = hashlib.sha256(body).hexdigest()
        if body_sha != url_sha:
            self._error(409, "body sha256 does not match URL")
            return

        try:
            validate_sidecar(meta)
        except SidecarError as e:
            self._error(422, f"sidecar invalid: {e}")
            return

        if meta["device_id"] != device_id:
            self._error(403, "token belongs to a different device")
            return
        if meta["sha256"] != body_sha:
            self._error(409, "meta.sha256 does not match body")
            return

        try:
            result = store_chunk(self.app.conn(), self.app.archive_dir, meta, meta_raw, body)
        except Exception as e:  # storage failure: the edge keeps the chunk and retries
            log.exception("store failed for %s", body_sha)
            self._error(500, f"store failed: {e.__class__.__name__}")
            return

        self._send(
            201 if result.created else 200,
            {
                "sha256": result.sha256,
                "status": "created" if result.created else "exists",
                "path": result.path,
            },
        )


def make_server(cfg: Config, bind: str | None = None,
                tokens: dict[str, str] | None = None) -> ThreadingHTTPServer:
    app = IngestApp(cfg, tokens=tokens)
    host, _, port = (bind or cfg.bind).rpartition(":")
    handler = type("BoundIngestHandler", (IngestHandler,), {"app": app})
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), handler)
    server.daemon_threads = True
    return server


def serve(cfg: Config, bind: str | None = None) -> None:
    server = make_server(cfg, bind=bind)
    log.info("ingest listening on %s:%d", *server.server_address[:2])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
