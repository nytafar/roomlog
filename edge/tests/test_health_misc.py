import json
import socket
import subprocess
import time
from collections import namedtuple

from roomlog_edge import clocksync, sdnotify
from roomlog_edge.cli import build_parser, main
from roomlog_edge.health import evaluate, render_prom, run_health
from roomlog_edge.status import read_status, write_status

Usage = namedtuple("Usage", "total used free")
OK_DISK = Usage(10**12, 10**11, 9 * 10**11)


def _capture_status(cfg, age_s=1.0, **over):
    now = time.time_ns()
    data = {"epoch": 2, "last_frame_age_s": 0.03, "rms_dbfs": -40.0, "chunks_dropped": 0,
            "clock_synced": True, "written_utc_ns": now - int(age_s * 1e9)}
    data.update(over)
    write_status(cfg.capture_status, data)


def _uploader_status(cfg, age_s=1.0, **over):
    now = time.time_ns()
    data = {"pending_files": 0, "last_success_utc_ns": now - 5 * 10**9, "last_error": None,
            "consecutive_failures": 0, "written_utc_ns": now - int(age_s * 1e9)}
    data.update(over)
    write_status(cfg.uploader_status, data)


def test_status_write_is_atomic_and_readable(cfg):
    write_status(cfg.capture_status, {"a": 1})
    assert read_status(cfg.capture_status)["a"] == 1
    assert "written_utc_ns" in read_status(cfg.capture_status)
    assert not cfg.capture_status.with_name("capture.json.tmp").exists()
    assert read_status(cfg.status_dir / "missing.json") is None


def test_healthy_when_everything_fresh(cfg):
    _capture_status(cfg)
    _uploader_status(cfg)
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert r.healthy, r.reasons
    assert r.metrics["roomlog_clock_synced"] == 1 and r.metrics["roomlog_epoch"] == 2
    assert r.metrics["roomlog_healthy"] == 1
    text = render_prom(r.metrics)
    assert "roomlog_last_frame_age_s" in text and "roomlog_spool_files 0" in text


def test_unhealthy_reasons(cfg, tmp_path):
    r = evaluate(cfg, ntp_state=False, disk_usage=OK_DISK)
    assert not r.healthy and "capture status missing" in r.reasons
    assert r.metrics["roomlog_clock_synced"] == 0
    _capture_status(cfg, age_s=120)
    _uploader_status(cfg)
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert any("stale" in x for x in r.reasons)
    _capture_status(cfg, last_frame_age_s=90)
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert any("last frame" in x for x in r.reasons)
    _capture_status(cfg)
    r = evaluate(cfg, ntp_state=True, disk_usage=Usage(100, 99, 1))
    assert any("disk low" in x for x in r.reasons)


def test_upload_stall(cfg):
    import os

    from roomlog_edge.spool import Spool

    _capture_status(cfg)
    sp = Spool(cfg.spool_dir)
    (sp.dir("pending") / "20260926T000000000Z_deadbeef.opus").write_bytes(b"x")
    (sp.dir("pending") / "20260926T000000000Z_deadbeef.json").write_bytes(b"{}")
    old = time.time() - 2 * 3600
    for f in sp.dir("pending").iterdir():
        os.utime(f, (old, old))
    _uploader_status(cfg, last_success_utc_ns=None)
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert any("without a successful upload" in x for x in r.reasons)
    _uploader_status(cfg)  # a recent success: fine even with old files
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert r.healthy, r.reasons
    _uploader_status(cfg, last_error="401: {}")
    r = evaluate(cfg, ntp_state=True, disk_usage=OK_DISK)
    assert any("auth" in x for x in r.reasons)


def test_run_health_writes_prom(cfg, monkeypatch):
    monkeypatch.setattr(clocksync, "ntp_synchronized", lambda: True)
    _capture_status(cfg)
    _uploader_status(cfg)
    rc = run_health(cfg)
    text = cfg.metrics_file.read_text()
    assert "roomlog_healthy" in text and "roomlog_spool_bytes" in text
    assert rc in (0, 1)


def test_ntp_synchronized_parses_and_falls_back(monkeypatch):
    def runner(out, rc=0):
        def run(*a, **k):
            return subprocess.CompletedProcess(a, rc, stdout=out, stderr="")
        return run

    monkeypatch.setattr(clocksync.shutil, "which", lambda _: "/usr/bin/timedatectl")
    assert clocksync.ntp_synchronized(runner("yes\n")) is True
    assert clocksync.ntp_synchronized(runner("no\n")) is False
    assert clocksync.ntp_synchronized(runner("", rc=1)) is None
    assert clocksync.ntp_synchronized(runner("weird")) is None
    monkeypatch.setattr(clocksync.shutil, "which", lambda _: None)
    assert clocksync.ntp_synchronized() is None


def test_sdnotify_writes_to_notify_socket(tmp_path, monkeypatch):
    path = tmp_path / "n.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(path))
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    assert sdnotify.ready() and sdnotify.watchdog() and sdnotify.status("hi")
    got = [srv.recv(64).decode() for _ in range(3)]
    assert got == ["READY=1", "WATCHDOG=1", "STATUS=hi"]
    srv.close()
    monkeypatch.delenv("NOTIFY_SOCKET")
    assert sdnotify.watchdog() is False
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))
    assert sdnotify.watchdog() is False


def test_cli_lists_subcommands(capsys):
    p = build_parser()
    help_text = p.format_help()
    for sub in ("capture", "upload", "health", "devices", "selftest"):
        assert sub in help_text
    assert main(["--config", "/nonexistent/edge.toml", "health"]) == 2
    assert "config error" in capsys.readouterr().err


def test_cli_health_with_config(cfg, tmp_path, monkeypatch, capsys):
    import tomllib  # noqa: F401 - stdlib present

    conf = tmp_path / "edge.toml"
    conf.write_text("\n".join([
        'device_id = "testbox"', 'server_url = "http://127.0.0.1:1"',
        f'token_file = "{cfg.token_file}"', f'spool_dir = "{cfg.spool_dir}"',
        f'model_path = "{cfg.model_path}"', f'status_dir = "{cfg.status_dir}"',
        f'metrics_file = "{cfg.metrics_file}"',
    ]))
    monkeypatch.setattr(clocksync, "ntp_synchronized", lambda: False)
    rc = main(["-c", str(conf), "health"])
    assert rc == 1
    assert "capture status missing" in capsys.readouterr().out
    assert json.loads("{}") == {}
    assert cfg.metrics_file.exists()
