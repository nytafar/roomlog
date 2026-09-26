"""Tiny stdlib HTTP client for the remote backends: multipart POST, JSON GET, error mapping."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from typing import Any

from .base import BackendError, BackendUnavailable

# 4xx codes that mean "not now" rather than "never": rate limit and request timeout.
TRANSIENT_4XX = {408, 429}


def encode_multipart(fields: dict[str, str], file_field: str, filename: str,
                     file_bytes: bytes, content_type: str = "audio/wav") -> tuple[bytes, str]:
    boundary = "----roomlog" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode()
    out += file_bytes
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def post_multipart(url: str, fields: dict[str, str], file_bytes: bytes, filename: str,
                   headers: dict[str, str] | None = None, timeout: float = 120.0) -> Any:
    body, ctype = encode_multipart(fields, "file", filename, file_bytes)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _do(req, timeout)


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return _do(req, timeout)


def _do(req: urllib.request.Request, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        if e.code >= 500 or e.code in TRANSIENT_4XX:
            raise BackendUnavailable(f"{req.full_url}: HTTP {e.code}: {detail}") from e
        raise BackendError(f"{req.full_url}: HTTP {e.code}: {detail}", status=e.code) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        raise BackendUnavailable(f"{req.full_url}: {e}") from e
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as e:
        raise BackendError(f"{req.full_url}: response is not JSON") from e
