import os
from collections import namedtuple

import pytest

from conftest import tone_pcm
from roomlog_edge import ogg, sidecar
from roomlog_edge.encode import Encoder, EncoderError, detect_backend, roomlog_tags
from roomlog_edge.spool import Spool, sha256_hex

RUN = "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b"


def test_encoder_writes_ogg_opus_with_roomlog_tags(encoder_available):
    enc = Encoder("auto")
    assert enc.backend in ("opusenc", "ffmpeg")
    pcm = tone_pcm(2.0)
    tags = roomlog_tags("oma", RUN, 3, 123456, "0.1.0")
    out = enc.encode(pcm, tags)
    assert out.startswith(b"OggS")
    info = ogg.parse(out)
    assert info.channels == 1 and info.input_sample_rate == 16000
    for k, v in tags.items():
        assert info.tags[k] == v
    assert abs(info.duration_s - 2.0) < 0.1
    assert len(out) < 2.0 * 24_000 / 8 * 1.6  # roughly 24 kbps plus container overhead


def test_encoder_rejects_bad_input(encoder_available):
    enc = Encoder("auto")
    with pytest.raises(EncoderError):
        enc.encode(b"", {})
    with pytest.raises(EncoderError):
        enc.encode(b"\x00", {})


def test_detect_backend_errors():
    with pytest.raises(EncoderError):
        detect_backend("nope")
    with pytest.raises(EncoderError):
        detect_backend("lame")


def test_ogg_parser_rejects_garbage():
    with pytest.raises(ogg.OggError):
        ogg.parse(b"not an ogg file")


def _meta(sha="0" * 64, start="2026-09-26T10:15:32.417Z", **over):
    m = sidecar.build(device_id="oma", sha256=sha, utc_ns=0, n_start=0, n_samples=16000, run_id=RUN,
                      epoch=0, discontinuity=False, clock_step=False, clock_synced=True,
                      cut_reason="silence", vad={}, edge_version="0.1.0")
    m["start_utc"] = start
    m.update(over)
    return m


def test_spool_write_rename_and_naming(tmp_path):
    sp = Spool(tmp_path / "spool")
    data = b"OggS-fake-bytes"
    e = sp.write(data, _meta())
    sha = sha256_hex(data)
    assert e.stem == f"20260926T101532417Z_{sha[:8]}"
    assert e.opus.parent.name == "pending" and e.json.exists()
    assert e.read_meta()["sha256"] == sha
    assert sidecar.validate(e.read_meta()) == []
    assert list((tmp_path / "spool" / "tmp").iterdir()) == []
    st = sp.stats()
    assert st.pending_files == 1 and st.pending_bytes > len(data)


def test_spool_lexical_order_is_time_order(tmp_path):
    sp = Spool(tmp_path / "spool")
    sp.write(b"b", _meta(start="2026-09-26T10:15:33.000Z"))
    sp.write(b"a", _meta(start="2026-09-26T10:15:32.000Z"))
    sp.write(b"c", _meta(start="2026-09-27T00:00:00.000Z"))
    stems = [e.stem for e in sp.entries("pending")]
    assert stems == sorted(stems)
    assert stems[0].startswith("20260926T101532000Z")


def test_spool_cleanup_tmp_and_move(tmp_path):
    sp = Spool(tmp_path / "spool")
    (sp.dir("tmp") / "x.opus.tmp").write_bytes(b"junk")
    (sp.dir("tmp") / "x.json.tmp").write_bytes(b"junk")
    assert sp.cleanup_tmp() == 2
    e = sp.write(b"data", _meta())
    f = sp.move(e, "failed")
    assert f.opus.parent.name == "failed" and not e.opus.exists()
    assert sp.stats().failed_files == 1 and sp.stats().pending_files == 0
    sp.delete(f)
    assert sp.stats().failed_files == 0


def test_spool_rewrite_meta_restamps_and_renames(tmp_path):
    sp = Spool(tmp_path / "spool")
    e = sp.write(b"data", _meta(clock_synced=False), dest="unsynced")
    meta = e.read_meta()
    meta["start_utc"] = "2026-09-26T11:00:00.000Z"
    meta["clock_synced"] = True
    n = sp.rewrite_meta(e, meta, "pending")
    assert n.stem.startswith("20260926T110000000Z_")
    assert n.read_meta()["clock_synced"] is True
    assert not e.opus.exists() and not e.json.exists()
    assert sp.entries("unsynced") == []


Usage = namedtuple("Usage", "total used free")


def test_disk_guard(tmp_path):
    sp = Spool(tmp_path / "spool", max_bytes=100, min_free_fraction=0.05)
    assert sp.disk_ok(Usage(1000, 500, 500))
    assert not sp.disk_ok(Usage(1000, 960, 40))
    sp.write(b"x" * 200, _meta())
    assert not sp.disk_ok(Usage(1000, 500, 500))
    assert sp.stats().pending_files == 1  # nothing was deleted
    assert os.path.exists(sp.entries("pending")[0].opus)
