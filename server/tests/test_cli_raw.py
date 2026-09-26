"""CLI coverage for the raw path: segment, resegment, purge-raw, status, verify, fetch-model, selftest."""

from __future__ import annotations

import hashlib
import json

import pytest

from roomlog_server import db as dbmod
from roomlog_server.cli import main
from roomlog_server.models import blob_sha1, fetch_vad_model, vad_sha256_file
from roomlog_server.times import ms_to_iso

from conftest import RAW_T0, SEGMENT, RawSegments, ffmpeg_required, make_config, pattern_pcm

DAY = 86_400_000


def run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def raw_site(tmp_path):
    """A config file with the energy VAD and a fake backend, plus three raw segments in the DB."""
    ffmpeg_required()
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(pattern_pcm([("s", 25), ("t", 15), ("s", 35)]), last_cut="shutdown")
    conn.close()
    toml = cfg.config_dir / "server.toml"
    toml.write_text(
        f'[paths]\ndata_dir = "{cfg.data_dir}"\n'
        '[segmenter]\nvad = "energy"\n'
        '[[backends]]\nname = "fake"\ntype = "fake"\nmodel = "fake-model"\n'
    )
    return cfg, fx, ["-c", str(toml)]


def test_cli_help_lists_raw_subcommands(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("segment", "resegment", "purge-raw"):
        assert cmd in out


def test_cli_segment_status_verify_and_worker(raw_site, capsys):
    cfg, fx, base = raw_site
    rc, out, _ = run(base + ["status"], capsys)
    assert rc == 0
    assert "raw            pending=3 segmented=0 derived=0" in out
    assert "s22" in out and "raw=3 raw_pending=3 last_raw=2026-09-26T13:01:00.000Z" in out
    assert "last=-" in out  # no chunks yet for this device

    rc, out, _ = run(base + ["segment"], capsys)
    assert rc == 0 and out.strip() == "epochs=1 chunks=1"
    rc, out, _ = run(base + ["segment"], capsys)
    assert rc == 0 and out.strip() == "epochs=0 chunks=0"

    rc, out, _ = run(base + ["--json", "status"], capsys)
    st = json.loads(out)
    assert st["raw"]["pending"] == 0 and st["raw"]["segmented"] == 3 and st["raw"]["derived_chunks"] == 1
    assert st["raw"]["bytes"] > 0
    assert st["chunks"]["pending"] == 1
    d = st["devices"][0]
    assert (d["device_id"], d["n_chunks"], d["n_derived"], d["n_raw"], d["n_raw_pending"]) == ("s22", 1, 1, 3, 0)
    rc, out, _ = run(base + ["status", "--prom"], capsys)
    assert 'roomlog_raw_segments_total{status="segmented"} 3' in out
    assert 'roomlog_device_raw_pending{device_id="s22"} 0' in out
    assert 'roomlog_device_last_raw_utc_seconds{device_id="s22"}' in out
    assert 'roomlog_device_last_chunk_utc_seconds{device_id="s22"}' in out

    rc, out, _ = run(base + ["verify"], capsys)
    assert rc == 0
    assert out.startswith("files=3 rows=3 problems=0 derived=1")

    rc, out, _ = run(base + ["worker", "--once"], capsys)
    assert rc == 0 and out.startswith("claimed=1 done=1 failed=0 windows=1 segmented=0")
    rc, out, _ = run(base + ["search", "ord00"], capsys)
    assert rc == 0 and "ord00" in out
    rc, out, _ = run(base + ["sessions"], capsys)
    assert rc == 0 and "s22_20260926T130024692Z" in out


def test_cli_worker_once_segments_then_transcribes(raw_site, capsys):
    cfg, fx, base = raw_site
    rc, out, _ = run(base + ["worker", "--once"], capsys)
    assert rc == 0 and out.startswith("claimed=1 done=1 failed=0 windows=1 segmented=1")


def test_cli_resegment(raw_site, capsys):
    cfg, fx, base = raw_site
    run(base + ["segment"], capsys)
    run(base + ["worker", "--once"], capsys)
    conn = dbmod.connect(cfg.db_path)
    sha_before = conn.execute("SELECT sha256 FROM chunks").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 1
    conn.close()
    rc, out, _ = run(base + ["resegment", "--device", "s22"], capsys)
    assert rc == 0
    assert out.splitlines() == ["reset epochs=1 raw=3 chunks_deleted=1", "epochs=1 chunks=1"]
    conn = dbmod.connect(cfg.db_path)
    row = conn.execute("SELECT sha256, status FROM chunks").fetchone()
    assert row["sha256"] == sha_before and row["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 0
    conn.close()
    rc, out, _ = run(base + ["resegment", "--device", "s22", "--from", "2027-01-01"], capsys)
    assert rc == 0 and out.splitlines()[0] == "reset epochs=0 raw=0 chunks_deleted=0"
    with pytest.raises(SystemExit):
        main(base + ["resegment"])


def test_cli_purge_raw(raw_site, capsys):
    cfg, fx, base = raw_site
    # an old, silent epoch from another device
    conn = dbmod.connect(cfg.db_path)
    old = RawSegments(cfg, conn, device_id="old", start_utc_ms=RAW_T0 - 400 * DAY,
                      received_utc_ms=RAW_T0 - 400 * DAY + 40_000)
    old.store_all(pattern_pcm([("s", 30), ("s", 5)]), last_cut="shutdown")
    conn.close()
    run(base + ["segment"], capsys)
    rc, out, _ = run(base + ["purge-raw"], capsys)
    assert rc == 0
    import re
    m = re.fullmatch(r"deleted=2 bytes=(\d+) retention_days=30", out.strip())
    assert m and int(m.group(1)) > 0
    conn = dbmod.connect(cfg.db_path)
    assert [r[0] for r in conn.execute("SELECT DISTINCT device_id FROM raw_segments")] == ["s22"]
    conn.close()
    assert not list((cfg.archive_dir / "raw").rglob("*_" + old.metas[0]["sha256"][:8] + ".opus"))
    rc, out, _ = run(base + ["verify"], capsys)
    assert rc == 0


def test_cli_verify_reports_raw_problems(raw_site, capsys):
    cfg, fx, base = raw_site
    run(base + ["segment"], capsys)
    raw_opus = next((cfg.archive_dir / "raw").rglob("*.opus"))
    raw_opus.write_bytes(b"corrupt")
    conn = dbmod.connect(cfg.db_path)
    conn.execute("DELETE FROM raw_segments WHERE n_start = ?", (SEGMENT,))
    conn.close()
    rc, out, _ = run(base + ["verify"], capsys)
    assert rc == 1
    assert "sha256 mismatch" in out
    assert "no raw_segments row" in out
    assert "raw audio missing" in out  # the derived chunk spans into the deleted segment


def test_fetch_vad_model_pins_by_blob_sha1(tmp_path):
    cfg = make_config(tmp_path)
    data = b"not really an onnx graph" * 100
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return data

    with pytest.raises(RuntimeError, match="blob sha1 mismatch"):
        fetch_vad_model(cfg, fetch=fetch)
    assert not cfg.vad_model_path.exists()
    path = fetch_vad_model(cfg, fetch=fetch, expected_blob_sha1=blob_sha1(data))
    assert path == cfg.vad_model_path and path.read_bytes() == data
    assert vad_sha256_file(path).read_text().strip() == hashlib.sha256(data).hexdigest()
    assert "silero-vad/v6.2.3/" in calls[-1]
    # a good file is kept without a download
    n = len(calls)
    assert fetch_vad_model(cfg, fetch=fetch, expected_blob_sha1=blob_sha1(data)) == path
    assert len(calls) == n
    assert not list(path.parent.glob("*.tmp"))


def test_selftest_reports_vad_model(raw_site, capsys):
    cfg, fx, base = raw_site
    (cfg.config_dir / "tokens.toml").write_text('s22 = "t1"\n')
    rc, out, _ = run(base + ["selftest", "--no-transcribe"], capsys)
    assert "[ok] vad model: energy gate" in out
    toml = cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{cfg.data_dir}"\n[[backends]]\nname = "fake"\ntype = "fake"\n')
    rc, out, _ = run(base + ["selftest", "--no-transcribe"], capsys)
    assert rc == 1
    assert "[FAIL] vad model:" in out and "3 raw segments waiting" in out
    conn = dbmod.connect(cfg.db_path)
    conn.execute("UPDATE raw_segments SET status = 'segmented'")
    conn.close()
    rc, out, _ = run(base + ["selftest", "--no-transcribe"], capsys)
    assert "[ok] vad model:" in out and "missing" in out
    cfg.vad_model_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.vad_model_path.write_bytes(b"x")
    rc, out, _ = run(base + ["selftest", "--no-transcribe"], capsys)
    assert f"[ok] vad model: {cfg.vad_model_path}" in out


def test_segment_command_reports_missing_model(raw_site, capsys):
    cfg, fx, base = raw_site
    toml = cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{cfg.data_dir}"\n')
    rc, out, _ = run(base + ["segment"], capsys)
    assert rc == 1
    assert out.startswith("epochs=0 chunks=0") and "fetch-model" in out
    assert ms_to_iso(RAW_T0) == "2026-09-26T13:00:00.000Z"
