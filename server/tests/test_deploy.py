"""Regression tests: packaging bound, ingest unit bind wait, installer defaults (review items 11, 12)."""

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


# ---------------------------------------------------------------- 11 + 12. packaging and unit


def test_mcp_dependency_bound():
    data = tomllib.loads((REPO_ROOT / "server" / "pyproject.toml").read_text())
    dep = next(d for d in data["project"]["dependencies"] if d.startswith("mcp"))
    assert dep == "mcp>=2,<3"


def test_ingest_unit_waits_for_bind_address():
    import os
    import subprocess

    unit = (REPO_ROOT / "deploy" / "server" / "roomlog-ingest.service").read_text()
    pre = next(l for l in unit.splitlines() if l.startswith("ExecStartPre="))
    assert pre == "ExecStartPre=/bin/bash %h/services/apps/roomlog/deploy/server/wait-for-bind.sh 120"
    assert unit.index("ExecStartPre=") < unit.index("ExecStart=%h")
    script = REPO_ROOT / "deploy" / "server" / "wait-for-bind.sh"
    assert os.access(script, os.X_OK)

    def run(bind, timeout="1"):
        env = dict(os.environ, ROOMLOG_BIND=bind)
        return subprocess.run(["bash", str(script), timeout], env=env, capture_output=True, text=True, timeout=20)

    assert run("127.0.0.1:8480").returncode == 0
    assert run("0.0.0.0:8480").returncode == 0
    assert run("").returncode == 0
    r = run("192.0.2.1:8480")  # TEST-NET, never on this box
    assert r.returncode == 1 and "192.0.2.1" in r.stderr
    # an address that exists on this box passes without waiting
    ips = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout
    first = next((l.split()[3].split("/")[0] for l in ips.splitlines() if "127.0.0.1" not in l), None)
    if first:
        assert run(f"{first}:8480").returncode == 0


def test_installer_defaults_and_fetch_model_tolerance():
    sh = (REPO_ROOT / "deploy" / "server" / "install.sh").read_text()
    assert 'https://github.com/nytafar/roomlog.git' in sh
    assert 'BRANCH="${ROOMLOG_BRANCH:-main}"' in sh
    assert 'elif ! run "$ROOMLOG" fetch-model; then' in sh
