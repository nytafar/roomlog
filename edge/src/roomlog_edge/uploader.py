"""Uploader: ``spool/pending/`` → ``PUT /v1/chunks/{sha256}`` → delete (design §3.6).

stdlib ``http.client`` only. One request per file, oldest first, actions per
status as in the contract, ``min(max_backoff, 2**k)`` seconds with full jitter
on retryable failures, reset on success.
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import sdnotify, sidecar
from .spool import Entry, Spool
from .status import write_status

RETRY = "retry"
ACK = "ack"
FAIL = "fail"


@dataclass(frozen=True)
class Outcome:
    action: str  # ack | fail | retry
    status: int | None
    detail: str


def classify(status: int) -> str:
    if status in (200, 201):
        return ACK
    if status in (409, 413, 422):
        return FAIL
    return RETRY  # 401, 403, 5xx and anything unexpected


def backoff_s(k: int, max_backoff_s: float = 300.0, rng: random.Random | None = None) -> float:
    cap = min(max_backoff_s, float(2 ** min(k, 30)))
    return (rng or random).uniform(0, cap)


class Client:
    def __init__(self, base_url: str, token: str, timeout_s: float = 30.0):
        u = urlsplit(base_url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError(f"bad server_url {base_url!r}")
        self.scheme, self.netloc, self.prefix = u.scheme, u.netloc, u.path.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def _conn(self):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.netloc, timeout=self.timeout_s)

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        h = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        conn = self._conn()
        try:
            conn.request(method, self.prefix + path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            conn.close()

    def put_chunk(self, opus: bytes, meta: dict) -> tuple[int, bytes]:
        headers = {
            "Content-Type": "audio/ogg",
            "Content-Length": str(len(opus)),
            "X-Roomlog-Meta": sidecar.dumps_wire(meta),
        }
        return self._request("PUT", f"/v1/chunks/{meta['sha256']}", opus, headers)

    def whoami(self) -> tuple[int, bytes]:
        return self._request("GET", "/v1/whoami")


class Uploader:
    def __init__(self, spool: Spool, client: Client, max_backoff_s: float = 300.0,
                 idle_poll_s: float = 5.0, status_path=None, rng: random.Random | None = None):
        self.spool = spool
        self.client = client
        self.max_backoff_s = max_backoff_s
        self.idle_poll_s = idle_poll_s
        self.status_path = status_path
        self.rng = rng or random.Random()
        self.failures = 0
        self.last_success_ns: int | None = None
        self.last_error: str | None = None
        self.uploaded_total = 0
        self.failed_total = 0

    def upload_one(self, entry: Entry) -> Outcome:
        try:
            meta = entry.read_meta()
            opus = entry.opus.read_bytes()
        except (OSError, ValueError) as e:
            self.spool.move(entry, "failed")
            return Outcome(FAIL, None, f"unreadable: {e}")
        try:
            status, body = self.client.put_chunk(opus, meta)
        except (OSError, http.client.HTTPException, socket.timeout) as e:
            return Outcome(RETRY, None, f"{type(e).__name__}: {e}")
        action = classify(status)
        detail = body[:200].decode("utf-8", "replace")
        if action == ACK:
            try:
                got = json.loads(body).get("sha256")
            except (ValueError, AttributeError):
                got = None
            if got != meta["sha256"]:
                return Outcome(RETRY, status, f"response sha256 mismatch: {got}")
            self.spool.delete(entry)
        elif action == FAIL:
            self.spool.move(entry, "failed")
        return Outcome(action, status, detail)

    def run_once(self, max_files: int | None = None) -> list[Outcome]:
        """Upload pending files in order until one needs a retry."""
        outcomes = []
        for i, entry in enumerate(self.spool.entries("pending")):
            if max_files is not None and i >= max_files:
                break
            o = self.upload_one(entry)
            outcomes.append(o)
            if o.action == ACK:
                self.failures = 0
                self.last_success_ns = time.time_ns()
                self.last_error = None
                self.uploaded_total += 1
            elif o.action == FAIL:
                self.failed_total += 1
                self.last_error = f"{o.status}: {o.detail}"
            else:
                self.failures += 1
                self.last_error = f"{o.status}: {o.detail}"
                break
        return outcomes

    def next_delay_s(self, outcomes: list[Outcome]) -> float:
        if outcomes and outcomes[-1].action == RETRY:
            return backoff_s(self.failures, self.max_backoff_s, self.rng)
        if outcomes:
            return 0.0
        return self.idle_poll_s

    def write_status(self) -> None:
        if self.status_path is None:
            return
        st = self.spool.stats()
        write_status(self.status_path, {
            "pending_files": st.pending_files,
            "pending_bytes": st.pending_bytes,
            "unsynced_files": st.unsynced_files,
            "failed_files": st.failed_files,
            "last_success_utc_ns": self.last_success_ns,
            "last_error": self.last_error,
            "consecutive_failures": self.failures,
            "uploaded_total": self.uploaded_total,
            "failed_total": self.failed_total,
        })

    def run_forever(self, stop) -> None:
        """``stop`` is a ``threading.Event``; the watchdog is fed once per loop."""
        sdnotify.ready()
        while not stop.is_set():
            outcomes = self.run_once()
            self.write_status()
            sdnotify.watchdog()
            delay = self.next_delay_s(outcomes)
            if delay > 0:
                stop.wait(delay)
        sdnotify.stopping()
