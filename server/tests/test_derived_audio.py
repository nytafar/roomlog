"""Playable files for derived chunks: written at segment time, removed with their rows,
backfilled for old rows, served by the recall reader like a speech chunk."""

from __future__ import annotations

import http.client
import sqlite3
import threading

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server import derived_audio, segmenter as segmod
from roomlog_server.audio import decode_opus, encode_opus
from roomlog_server.derived_audio import DERIVED_PREFIX, backfill, sweep
from roomlog_server.reader_http import ReaderServer
from roomlog_server.recall import Reader
from roomlog_server.segmenter import EnergyVad, Segmenter, pcm_int16, reset_segmentation
from roomlog_server.verify import verify_archive

from conftest import RATE, RAW_T0, SEGMENT, RawSegments, ffmpeg_required, make_config

W = 512


@pytest.fixture(autouse=True)
def _need_ffmpeg():
    ffmpeg_required()


def noise_pcm(pattern: list[tuple[str, float]], amp: int = 8000, seed: int = 7) -> np.ndarray:
    """`s` = silence, `n` = white noise: aperiodic, so a time shift shows in a correlation."""
    rng = np.random.default_rng(seed)
    parts = []
    for kind, seconds in pattern:
        n = int(round(seconds * RATE))
        parts.append((rng.standard_normal(n) * amp / 3).clip(-32000, 32000).astype(np.int16)
                     if kind == "n" else np.zeros(n, dtype=np.int16))
    return np.concatenate(parts)


def make_segmenter(cfg, conn, now_ms: int = RAW_T0 + 10_000_000) -> Segmenter:
    return Segmenter(conn, cfg.archive_dir, EnergyVad, vad_info={"model": "energy"}, now=lambda: now_ms)


def derived(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM chunks WHERE kind = 'derived' ORDER BY n_start").fetchall()


def files(cfg) -> set[str]:
    root = cfg.archive_dir / DERIVED_PREFIX
    return {str(p.relative_to(cfg.archive_dir)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def raw_stream(cfg, conn) -> np.ndarray:
    paths = [r[0] for r in conn.execute("SELECT path FROM raw_segments ORDER BY n_start")]
    return np.concatenate([decode_opus(cfg.archive_dir / p) for p in paths])


def best_lag(a: np.ndarray, b: np.ndarray, span: int = 400) -> int:
    """The shift of `b` against `a` with the highest correlation, within ±span samples."""
    core = slice(span, len(a) - span)
    scores = {lag: float(np.dot(a[core], b[span + lag: len(a) - span + lag])) for lag in range(-span, span + 1)}
    return max(scores, key=scores.get)


@pytest.fixture
def site(tmp_path):
    """Two speech bursts in one epoch: one inside a segment, one across the 30 s seam."""
    cfg = make_config(tmp_path)
    conn = dbmod.connect(cfg.db_path)
    fx = RawSegments(cfg, conn)
    fx.store_all(noise_pcm([("s", 5), ("n", 6), ("s", 14), ("n", 15), ("s", 35)]), last_cut="shutdown")
    yield cfg, conn, fx
    conn.close()


def test_segmenter_writes_each_chunk_file_with_exactly_its_samples(site):
    cfg, conn, _ = site
    res = make_segmenter(cfg, conn).run_once()
    assert res.errors == [] and res.chunks == 2
    stream = raw_stream(cfg, conn)
    rows = derived(conn)
    assert rows[1]["n_start"] < SEGMENT < rows[1]["n_start"] + rows[1]["n_samples"]  # straddles the seam
    assert files(cfg) == {r["path"] for r in rows}
    for r in rows:
        rel = r["path"]
        assert rel.startswith(DERIVED_PREFIX) and rel.endswith(f"_{r['sha256'][:8]}.opus")
        assert rel == derived_audio.derived_relpath(r["start_utc_ms"], r["sha256"])
        got = decode_opus(cfg.archive_dir / rel)
        want = stream[r["n_start"]: r["n_start"] + r["n_samples"]]
        assert len(got) == r["n_samples"]  # sample-exact: the reader's time zero is the first sample
        assert best_lag(want, got) == 0
        assert np.corrcoef(want, got)[0, 1] > 0.6  # 24 kbps speech codec on white noise
        # the file is a function of the PCM its sha256 names
        assert (cfg.archive_dir / rel).read_bytes() == encode_opus(pcm_int16(want))
    assert verify_archive(conn, cfg.archive_dir)["ok"]


def test_a_crash_between_file_and_commit_leaves_a_file_the_rerun_reuses(site, monkeypatch):
    cfg, conn, _ = site

    def crash(self, chain, p):
        raise RuntimeError("power cut")

    monkeypatch.setattr(Segmenter, "_insert_derived", crash)
    res = make_segmenter(cfg, conn).run_once()
    assert res.errors and derived(conn) == []
    left = files(cfg)
    assert len(left) == 2  # every file of the pass is written before the lock, then the commit failed
    monkeypatch.undo()
    assert make_segmenter(cfg, conn).run_once().chunks == 2
    assert files(cfg) == {r["path"] for r in derived(conn)} >= left


def test_a_file_swept_before_the_lock_is_written_again_under_it(site, monkeypatch):
    cfg, conn, _ = site
    real = segmod.ensure_file
    calls = []

    def racing(archive_dir, rel, body):
        n = real(archive_dir, rel, body)
        calls.append(rel)
        if calls.count(rel) == 1:  # the pre-lock write, then a sweep removes it
            (archive_dir / rel).unlink()
        return n

    monkeypatch.setattr(segmod, "ensure_file", racing)
    assert make_segmenter(cfg, conn).run_once().chunks == 2
    for r in derived(conn):
        assert (cfg.archive_dir / r["path"]).is_file()


def test_sweep_removes_only_unreferenced_files_and_old_temps(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    kept = files(cfg)
    stray = cfg.archive_dir / DERIVED_PREFIX / "2026/09/26/20260926T130000000Z_deadbeef.opus"
    stray.write_bytes(b"orphan")
    tmp = stray.with_name(stray.name + ".x.tmp")
    tmp.write_bytes(b"half")
    assert sweep(conn, cfg.archive_dir)["removed"] == 0  # too young: a pass may be about to commit it
    out = sweep(conn, cfg.archive_dir, min_age_s=0)
    assert (out["removed"], out["bytes"], out["tmp_removed"]) == (1, 6, 1)
    assert files(cfg) == kept


def test_reset_removes_files_and_rederived_chunks_get_new_ids(site):
    cfg, conn, fx = site
    make_segmenter(cfg, conn).run_once()
    before = derived(conn)
    max_id = max(r["id"] for r in before)
    # reset only the later burst (raw segments from 30 s on): its chunk goes, the first stays
    out = reset_segmentation(conn, "s22", from_ms=RAW_T0 + 30_000, archive_dir=cfg.archive_dir)
    assert (out["chunks_deleted"], out["audio_removed"]) == (1, 1)
    assert files(cfg) == {before[0]["path"]}
    make_segmenter(cfg, conn).run_once()
    after = derived(conn)
    assert [r["sha256"] for r in after] == [r["sha256"] for r in before]
    assert after[1]["id"] > max_id  # never the deleted chunk's id (schema v6)
    assert files(cfg) == {r["path"] for r in after}


def test_reset_with_a_changed_derivation_leaves_no_orphans(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    old = {r["path"] for r in derived(conn)}
    reset_segmentation(conn, "s22", archive_dir=cfg.archive_dir)
    assert files(cfg) == set()
    seg = make_segmenter(cfg, conn)
    seg.chunker_kwargs = {"pad_ms": 100}  # a different chunker: other boundaries, other files
    seg.run_once()
    new = {r["path"] for r in derived(conn)}
    assert new and not (new & old) and files(cfg) == new


def test_reset_without_archive_dir_leaves_files_for_the_sweep(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    assert reset_segmentation(conn, "s22")["audio_removed"] == 0
    assert len(files(cfg)) == 2
    assert not verify_archive(conn, cfg.archive_dir)["ok"]  # the orphans show up
    assert sweep(conn, cfg.archive_dir, min_age_s=0)["removed"] == 2
    assert files(cfg) == set()


def test_rederiving_a_row_from_before_files_adopts_the_file(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    rows = derived(conn)
    conn.execute("UPDATE chunks SET path = NULL")
    for r in rows:
        (cfg.archive_dir / r["path"]).unlink()
    # replay the pass as if progress had not been saved: the rows conflict on sha256
    conn.execute("DELETE FROM raw_progress")
    conn.execute("UPDATE raw_segments SET status = 'pending'")
    assert make_segmenter(cfg, conn).run_once().chunks == 0
    assert [r["path"] for r in derived(conn)] == [r["path"] for r in rows]
    assert files(cfg) == {r["path"] for r in rows}


# ---------------------------------------------------------------- backfill


def strip_files(cfg, conn) -> dict[int, bytes]:
    """Turn the segmenter's output into rows from before derived files; return the bytes."""
    rows = derived(conn)
    data = {r["id"]: (cfg.archive_dir / r["path"]).read_bytes() for r in rows}
    conn.execute("UPDATE chunks SET path = NULL WHERE kind = 'derived'")
    for r in rows:
        (cfg.archive_dir / r["path"]).unlink()
    return data


def test_backfill_writes_the_same_files_and_is_idempotent(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    paths = {r["id"]: r["path"] for r in derived(conn)}
    data = strip_files(cfg, conn)
    res = backfill(conn, cfg.archive_dir)
    assert (res.derived, res.present, res.linked, res.written, res.errors) == (2, 0, 2, 2, [])
    assert res.bytes == sum(len(b) for b in data.values())
    for r in derived(conn):
        assert r["path"] == paths[r["id"]]
        assert (cfg.archive_dir / r["path"]).read_bytes() == data[r["id"]]  # straddling one included
    again = backfill(conn, cfg.archive_dir)
    assert (again.present, again.linked, again.written, again.bytes) == (2, 0, 0, 0)
    assert verify_archive(conn, cfg.archive_dir)["ok"]


def test_backfill_resumes_and_repairs_a_missing_file(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    strip_files(cfg, conn)
    first = backfill(conn, cfg.archive_dir, limit=1)
    assert (first.linked, first.written) == (1, 1)
    rest = backfill(conn, cfg.archive_dir)
    assert (rest.present, rest.linked) == (1, 1)
    r = derived(conn)[0]
    (cfg.archive_dir / r["path"]).unlink()  # a row whose file was lost
    fixed = backfill(conn, cfg.archive_dir)
    assert (fixed.present, fixed.linked, fixed.written) == (1, 1, 1)
    assert (cfg.archive_dir / r["path"]).is_file()


def test_backfill_skips_chunks_it_cannot_reproduce(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    strip_files(cfg, conn)
    a, b = derived(conn)
    conn.execute("UPDATE chunks SET sha256 = ? WHERE id = ?", ("f" * 64, a["id"]))
    conn.execute("DELETE FROM raw_segments WHERE n_start = ?", (SEGMENT,))  # b straddles into it
    res = backfill(conn, cfg.archive_dir)
    assert (res.mismatched, res.raw_missing, res.linked) == (1, 1, 0)
    assert [r["path"] for r in derived(conn)] == [None, None] and files(cfg) == set()


def test_backfill_drops_the_file_of_a_chunk_deleted_meanwhile(site, monkeypatch):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    strip_files(cfg, conn)
    victim = derived(conn)[0]["id"]
    real = derived_audio.ensure_file
    other = dbmod.connect(cfg.db_path)  # the worker's resegment, on its own connection

    def racing(archive_dir, rel, body):
        n = real(archive_dir, rel, body)
        other.execute("DELETE FROM chunks WHERE id = ?", (victim,))
        return n

    monkeypatch.setattr(derived_audio, "ensure_file", racing)
    res = backfill(conn, cfg.archive_dir, limit=1)
    other.close()
    assert (res.gone, res.linked) == (1, 0)
    assert files(cfg) == set()


# ---------------------------------------------------------------- the reader


def test_reader_serves_a_derived_chunk(site):
    cfg, conn, _ = site
    make_segmenter(cfg, conn).run_once()
    c = derived(conn)[1]  # the one across the seam
    conn.execute("""INSERT INTO segments(chunk_id,device_id,idx,start_utc_ms,end_utc_ms,offset_ms,text,lang,model_id)
                    VALUES(?,'s22',0,?,?,0,'hei','no','m')""", (c["id"], c["start_utc_ms"], c["end_utc_ms"]))
    conn.execute("UPDATE chunks SET model_id = 'm', status = 'done' WHERE id = ?", (c["id"],))
    reader = Reader(cfg.db_path, archive_dir=cfg.archive_dir)
    from roomlog_server.recall import parse
    window = parse('transcript', [('device_id', 's22'), ('from_ms', str(RAW_T0)), ('to_ms', str(RAW_T0 + 90_000))])
    [item] = reader.read('transcript', window)['items']
    assert item['audio_available'] is True and item['chunk_start_utc_ms'] == c["start_utc_ms"]

    server = ReaderServer(('127.0.0.1', 0), reader, {'owner-secret': 'owner'})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        client.request('GET', f'/v1/recall/audio?chunk_id={c["id"]}', headers={'Authorization': 'Bearer owner-secret'})
        r = client.getresponse()
        body = r.read()
        assert r.status == 200 and r.getheader('Content-Type') == 'audio/ogg'
        assert r.getheader('ETag') == f'"{c["sha256"]}"'
        assert body == (cfg.archive_dir / c["path"]).read_bytes() and body.startswith(b"OggS")
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_encode_opus_is_deterministic_and_sample_exact(tmp_path):
    pcm = noise_pcm([("n", 1.234)])
    a, b = encode_opus(pcm), encode_opus(pcm)
    assert a == b and a.startswith(b"OggS")
    (tmp_path / "x.opus").write_bytes(a)
    assert len(decode_opus(tmp_path / "x.opus")) == len(pcm)
