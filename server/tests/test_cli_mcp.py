from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from roomlog_server import db as dbmod
from roomlog_server.archive import insert_chunk_row
from roomlog_server.cli import main
from roomlog_server.mcp_server import Tools, build_server
from roomlog_server.sessions import rebuild_sessions
from roomlog_server.times import iso_to_ms, ms_to_iso

from conftest import make_config, make_sidecar

T0 = iso_to_ms("2026-09-26T10:00:00.000Z")


def seed(cfg) -> dict:
    """Two devices, three sessions, a handful of Norwegian segments. Files exist in the archive."""
    conn = dbmod.connect(cfg.db_path)
    ids: dict[str, int] = {}

    def add(key, device, start_ms, dur_s, texts):
        body = f"{key}".encode() * 40
        meta = make_sidecar(body, device_id=device, start_utc=ms_to_iso(start_ms), duration_s=dur_s)
        sha = hashlib.sha256(body).hexdigest()
        from roomlog_server.archive import archive_relpath
        rel = archive_relpath(start_ms, sha)
        p = cfg.archive_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        p.with_suffix(".json").write_text(json.dumps(meta))
        cid = insert_chunk_row(conn, meta, json.dumps(meta), sha, rel)
        conn.execute("UPDATE chunks SET status='done', model_id='fake', transcribed_utc_ms=? WHERE id=?", (start_ms + 5000, cid))
        for i, (off, text) in enumerate(texts):
            conn.execute(
                "INSERT INTO segments (chunk_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang, model_id) VALUES (?,?,?,?,?,?,?,?)",
                (cid, i, start_ms + off, start_ms + off + 1500, off, text, "no", "fake"),
            )
        ids[key] = cid
        return cid

    add("a1", "oma", T0, 10, [(0, "Vi går hjem nå"), (3000, "Blåbær og øl på øya")])
    add("a2", "oma", T0 + 20_000, 10, [(500, "Møtet om budsjettet i morgen")])
    add("b1", "oma", T0 + 3_600_000, 8, [(0, "Kunstig intelligens er interessant")])
    add("w1", "pi-work", T0 + 100_000, 5, [(100, "Vi går til lunsj")])
    rebuild_sessions(conn, 300, now=T0 + 7_200_000)
    ids["segments"] = {r["text"]: r["id"] for r in conn.execute("SELECT id, text FROM segments")}
    conn.close()
    return ids


@pytest.fixture
def seeded(tmp_path):
    cfg = make_config(tmp_path)
    ids = seed(cfg)
    toml = cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{cfg.data_dir}"\n')
    return cfg, ids, ["-c", str(toml)]


def run(argv, capsys):
    rc = main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


# ------------------------------------------------------------- CLI


def test_cli_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("ingest", "worker", "search", "sessions", "session", "export", "status",
                "verify", "sessionize", "fetch-model", "selftest"):
        assert cmd in out


def test_cli_search(seeded, capsys):
    cfg, ids, base = seeded
    rc, out, _ = run(base + ["search", "går"], capsys)
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert all("går" in l for l in lines)
    assert lines[0].startswith(("oma_20260926T100000000Z", "pi-work_20260926T100140000Z"))
    assert "2026-09-26T10:00:00.000Z" in out
    rc, out, _ = run(base + ["search", "går", "--device", "pi-work"], capsys)
    assert out.strip().splitlines() == ["pi-work_20260926T100140000Z  2026-09-26T10:01:40.100Z  Vi går til lunsj"]
    rc, out, _ = run(base + ["search", "gar"], capsys)
    assert out.strip() == ""
    rc, out, _ = run(base + ["search", "intellig", "--fuzzy"], capsys)
    assert "Kunstig intelligens" in out
    rc, out, _ = run(base + ["search", "intellig"], capsys)
    assert out.strip() == ""
    rc, out, _ = run(base + ["search", "går", "--from", "2026-09-26T10:01:00Z", "--to", "2026-09-27"], capsys)
    assert out.strip().splitlines()[0].startswith("pi-work_")
    rc, out, _ = run(base + ["--json", "search", "øl", "--limit", "1"], capsys)
    payload = json.loads(out)
    assert payload[0]["text"] == "Blåbær og øl på øya"
    assert payload[0]["segment_id"] == ids["segments"]["Blåbær og øl på øya"]


def test_cli_sessions_and_session(seeded, capsys):
    cfg, ids, base = seeded
    rc, out, _ = run(base + ["sessions"], capsys)
    lines = out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("oma_20260926T110000000Z")  # newest first
    rc, out, _ = run(base + ["sessions", "--device", "oma", "--to", "2026-09-26T10:30:00Z"], capsys)
    assert out.strip().splitlines()[0].startswith("oma_20260926T100000000Z")
    assert "chunks=2 segments=3" in out
    rc, out, _ = run(base + ["session", "oma_20260926T100000000Z"], capsys)
    assert rc == 0
    assert "[10:00:00] Vi går hjem nå" in out
    assert "[10:00:03] Blåbær og øl på øya" in out
    assert "[10:00:20] Møtet om budsjettet i morgen" in out
    rc, out, err = run(base + ["session", "nope"], capsys)
    assert rc == 1


def test_cli_export_idempotent(seeded, capsys, tmp_path):
    cfg, ids, base = seeded
    vault = tmp_path / "vault"
    rc, out, _ = run(base + ["export", "--vault", str(vault)], capsys)
    assert rc == 0 and out.strip() == "written=3 unchanged=0"
    files = sorted(p.name for p in vault.glob("*.md"))
    assert files == ["oma_20260926T100000000Z.md", "oma_20260926T110000000Z.md", "pi-work_20260926T100140000Z.md"]
    text = (vault / "oma_20260926T100000000Z.md").read_text()
    assert text.startswith("---\nkind: roomlog-session\nsession_id: \"oma_20260926T100000000Z\"\ndevice_id: \"oma\"\n")
    assert "start_utc: \"2026-09-26T10:00:00.000Z\"" in text
    assert "closed: true" in text
    assert "[10:00:03] Blåbær og øl på øya" in text
    mtimes = {p.name: p.stat().st_mtime_ns for p in vault.glob("*.md")}
    rc, out, _ = run(base + ["export", "--vault", str(vault)], capsys)
    assert out.strip() == "written=0 unchanged=3"
    assert {p.name: p.stat().st_mtime_ns for p in vault.glob("*.md")} == mtimes
    rc, out, _ = run(base + ["export", "--vault", str(vault), "--device", "pi-work"], capsys)
    assert out.strip() == "written=0 unchanged=1"


def test_cli_status_and_prom(seeded, capsys, tmp_path):
    cfg, ids, base = seeded
    rc, out, _ = run(base + ["status"], capsys)
    assert rc == 0
    assert "pending=0 done=4 failed=0" in out
    assert "oma " in out and "pi-work" in out
    rc, out, _ = run(base + ["status", "--prom"], capsys)
    assert 'roomlog_chunks_total{status="done"} 4' in out
    assert 'roomlog_device_last_chunk_utc_seconds{device_id="pi-work"} 1790416900.0' in out
    assert "roomlog_sessions_total 3" in out
    target = tmp_path / "out" / "roomlog.prom"
    rc, out, _ = run(base + ["status", "--prom", "--write", str(target)], capsys)
    assert out == ""
    assert "roomlog_segments_total 5" in target.read_text()
    rc, out, _ = run(base + ["--json", "status"], capsys)
    st = json.loads(out)
    assert st["chunks"] == {"pending": 0, "done": 4, "failed": 0}
    assert [d["device_id"] for d in st["devices"]] == ["oma", "pi-work"]


def test_cli_verify(seeded, capsys):
    cfg, ids, base = seeded
    rc, out, _ = run(base + ["verify"], capsys)
    assert rc == 0
    assert out.startswith("files=4 rows=4 problems=0")
    # corrupt one file and orphan a row
    opus = next(cfg.archive_dir.rglob("*.opus"))
    opus.write_bytes(b"corrupted")
    conn = dbmod.connect(cfg.db_path)
    conn.execute("UPDATE chunks SET path='2026/09/26/missing.opus' WHERE id=?", (ids["w1"],))
    conn.close()
    rc, out, _ = run(base + ["verify"], capsys)
    assert rc == 1
    assert "sha256 mismatch" in out
    assert "row without file" in out


def test_cli_sessionize(seeded, capsys):
    cfg, ids, base = seeded
    conn = dbmod.connect(cfg.db_path)
    conn.execute("DELETE FROM sessions")
    conn.close()
    rc, out, _ = run(base + ["sessionize"], capsys)
    assert rc == 0 and out.strip() == "sessions=3"


def test_cli_worker_once_with_fake_backend(seeded, capsys, tmp_path):
    cfg, ids, base = seeded
    toml = cfg.config_dir / "server.toml"
    toml.write_text(
        f'[paths]\ndata_dir = "{cfg.data_dir}"\n[[backends]]\nname = "fake"\ntype = "fake"\nmodel = "fake-model"\n'
    )
    # nothing pending: exits clean
    rc, out, _ = run(base + ["worker", "--once"], capsys)
    assert rc == 0 and out.startswith("claimed=0")
    # a pending chunk whose 'audio' is junk: decode fails, attempt recorded, exit 1
    conn = dbmod.connect(cfg.db_path)
    conn.execute("UPDATE chunks SET status='pending' WHERE id=?", (ids["w1"],))
    conn.close()
    rc, out, _ = run(base + ["worker", "--once"], capsys)
    assert rc == 1 and "decode" in out


def test_cli_no_db_yet(tmp_path, capsys):
    cfg = make_config(tmp_path)
    toml = cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{cfg.data_dir}"\n')
    with pytest.raises(SystemExit):
        main(["-c", str(toml), "search", "x"])


def test_cli_selftest_probe_only(seeded, capsys):
    cfg, ids, base = seeded
    (cfg.config_dir / "tokens.toml").write_text('oma = "t1"\n')
    toml = cfg.config_dir / "server.toml"
    toml.write_text(
        f'[paths]\ndata_dir = "{cfg.data_dir}"\n[[backends]]\nname = "fake"\ntype = "fake"\n'
        f'[[backends]]\nname = "local"\ntype = "local"\nmodel = "NbAiLab/nb-whisper-medium"\n'
    )
    rc, out, _ = run(base + ["selftest", "--no-transcribe"], capsys)
    assert rc == 1  # local model is not fetched on this machine
    assert "[ok] db:" in out
    assert "[ok] tokens: 1 device token(s)" in out
    assert "[ok] probe fake" in out
    assert "[FAIL] probe local" in out
    assert "[FAIL] decode" in out  # seeded archive files are not real Ogg
    rc, out, _ = run(base + ["selftest"], capsys)
    assert "[ok] transcribe fake" in out


# ------------------------------------------------------------- MCP


def test_mcp_tools_in_process(seeded):
    cfg, ids, base = seeded
    t = Tools(cfg)
    hits = t.search("går")
    assert len(hits) == 2
    assert {h["device_id"] for h in hits} == {"oma", "pi-work"}
    assert t.search("går", device_id="pi-work")[0]["text"] == "Vi går til lunsj"
    assert t.search("går", from_utc="2026-09-26T10:01:00Z")[0]["device_id"] == "pi-work"
    assert t.search("går", to_utc="2026-09-26T10:01:00Z")[0]["device_id"] == "oma"
    assert t.search("gar") == []
    assert t.search("intellig", fuzzy=True)[0]["text"].startswith("Kunstig")
    assert t.search("går", limit=1, offset=1)[0]["segment_id"] != t.search("går", limit=1)[0]["segment_id"]

    sessions = t.list_sessions()
    assert [s["session_id"] for s in sessions] == [
        "oma_20260926T110000000Z", "pi-work_20260926T100140000Z", "oma_20260926T100000000Z"]
    assert [s["session_id"] for s in t.list_sessions(device_id="pi-work")] == ["pi-work_20260926T100140000Z"]
    assert len(t.list_sessions(from_utc="2026-09-26T10:30:00Z")) == 1
    assert len(t.list_sessions(limit=1)) == 1

    devices = t.list_devices()
    assert [d["device_id"] for d in devices] == ["oma", "pi-work"]
    assert devices[0]["n_chunks"] == 3 and devices[0]["n_sessions"] == 2
    assert devices[1]["last_chunk_utc"] == "2026-09-26T10:01:40.000Z"

    s = t.get_session("oma_20260926T100000000Z")
    assert s["n_segments"] == 3 and "segments" not in s
    assert s["transcript"].splitlines()[0] == "[10:00:00] Vi går hjem nå"
    assert s["truncated"] is False
    short = t.get_session("oma_20260926T100000000Z", max_chars=60)
    assert short["truncated"] is True
    assert "[... truncated ...]" in short["transcript"]
    assert short["transcript"].startswith("[10:00")
    with pytest.raises(ValueError):
        t.get_session("nope")

    seg_id = ids["segments"]["Møtet om budsjettet i morgen"]
    ctx = t.get_segment_context(seg_id, window_s=60)
    assert ctx["segment"]["text"] == "Møtet om budsjettet i morgen"
    assert [c["text"] for c in ctx["context"]] == ["Vi går hjem nå", "Blåbær og øl på øya", "Møtet om budsjettet i morgen"]
    ctx = t.get_segment_context(seg_id, window_s=5)
    assert [c["text"] for c in ctx["context"]] == ["Møtet om budsjettet i morgen"]
    with pytest.raises(ValueError):
        t.get_segment_context(999999)


def test_mcp_server_registers_tools_and_calls_them(seeded):
    cfg, ids, base = seeded
    server = build_server(cfg)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert names == {"search", "list_sessions", "list_devices", "get_session", "get_segment_context"}
    search_tool = next(t for t in tools if t.name == "search")
    props = getattr(search_tool, "input_schema", getattr(search_tool, "inputSchema", None))["properties"]
    assert set(props) == {"query", "from_utc", "to_utc", "device_id", "limit", "offset", "fuzzy"}

    def payload_of(result):
        sc = getattr(result, "structured_content", None)
        if sc is not None:
            return sc
        if isinstance(result, tuple):
            return result[1]
        texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
        return json.loads(texts[0]) if texts else result

    devices = payload_of(asyncio.run(server.call_tool("list_devices", {})))
    assert "pi-work" in json.dumps(devices, ensure_ascii=False)
    hits = payload_of(asyncio.run(server.call_tool("search", {"query": "øl", "device_id": "oma"})))
    assert "Blåbær og øl på øya" in json.dumps(hits, ensure_ascii=False)


def test_mcp_main_help(capsys):
    from roomlog_server.mcp_server import main as mcp_main
    with pytest.raises(SystemExit) as e:
        mcp_main(["--help"])
    assert e.value.code == 0
    assert "roomlog-mcp" in capsys.readouterr().out
