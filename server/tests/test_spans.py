"""ADR 0008: dictation spans replace STT for their window; tags carry a source; dictation
rows are hidden from default queries and shown on request."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

import numpy as np
import pytest

from roomlog_server import db as dbmod
from roomlog_server.archive import insert_chunk_row
from roomlog_server.backends.fake import FakeBackend
from roomlog_server.cli import main
from roomlog_server.db import fts_query
from roomlog_server.ingest import make_server
from roomlog_server.mcp_server import Tools
from roomlog_server.queries import get_segment_context, get_session, list_spans, search, session_segments, status
from roomlog_server.spans import SpanError, add_tag, store_span, validate_span
from roomlog_server.times import iso_to_ms, ms_to_iso
from roomlog_server.worker import DICTATION_MODEL_ID, Worker

from conftest import make_config, make_sidecar
from test_ingest import TOKENS, Client
from test_worker import SR, T0, Fixture

NOW = T0 + 10_000_000  # the fixture worker's clock: every chunk is long past the hold


def span_body(start_s: float, end_s: float, text: str = "restart the worker", lang: str = "en", **extra) -> dict:
    body = {
        "start_utc": ms_to_iso(T0 + int(start_s * 1000)),
        "end_utc": ms_to_iso(T0 + int(end_s * 1000)),
        "text": text,
        "lang": lang,
        "engine": "voxtype/parakeet-tdt-0.6b-v3-int8",
        "mode": "raw",
        "target": {"app": "ghostty", "window": "~/code/roomlog"},
        "cancelled": False,
        "origin": "dictation-span/1",
    }
    body.update(extra)
    return body


def post_span(conn: sqlite3.Connection, device_id: str, body: dict, now: int = NOW) -> int:
    span_id, created = store_span(conn, device_id, validate_span(body), json.dumps(body), now=now)
    assert created
    return span_id


def tags_of(conn: sqlite3.Connection, segment_id: int) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM tags WHERE target = 'segment' AND segment_id = ?", (segment_id,))}


# ------------------------------------------------------------- validation


def test_validate_span_normalizes_and_rejects():
    s = validate_span(span_body(0, 6.25))
    assert (s["start_utc_ms"], s["end_utc_ms"]) == (T0, T0 + 6250)
    assert s["app"] == "ghostty" and s["window"] == "~/code/roomlog"
    assert s["cancelled"] is False and s["device_id"] is None
    assert validate_span(span_body(0, 1, text="   "))["cancelled"] is True  # empty = cancelled
    assert validate_span(span_body(0, 1, lang="NO"))["lang"] == "no"
    minimal = {k: v for k, v in span_body(0, 1).items() if k not in ("target", "cancelled")}
    assert validate_span(minimal)["app"] is None
    for bad in (
        "not an object",
        {**span_body(0, 1), "start_utc": "2026-10-01T12:00:00Z"},   # not the sidecar form
        span_body(5, 4),                                              # end before start
        span_body(0, 3601),                                           # over an hour
        {**span_body(0, 1), "mode": "polish"},
        {**span_body(0, 1), "lang": "e n"},
        {**span_body(0, 1), "text": 42},
        {**span_body(0, 1), "cancelled": "yes"},
        {**span_body(0, 1), "target": "ghostty"},
        {k: v for k, v in span_body(0, 1).items() if k != "origin"},
        {**span_body(0, 1), "text": "x" * 20_001},
    ):
        with pytest.raises(SpanError):
            validate_span(bad)


# ------------------------------------------------------------- storage and tags


def test_store_span_is_idempotent_and_tags_the_time_span(conn):
    body = span_body(0, 6)
    span_id, created = store_span(conn, "nyta", validate_span(body), json.dumps(body), now=NOW)
    again, created_again = store_span(conn, "nyta", validate_span(body), json.dumps(body), now=NOW)
    assert created and not created_again and again == span_id
    row = conn.execute("SELECT * FROM dictation_spans WHERE id = ?", (span_id,)).fetchone()
    assert row["status"] == "pending" and row["device_id"] == "nyta" and row["lang"] == "en"
    tags = conn.execute("SELECT * FROM tags WHERE target = 'span' ORDER BY id").fetchall()
    assert [(t["key"], t["value"], t["source"], t["origin"]) for t in tags] == [
        ("channel", "dictation", "deterministic", "dictation-span/1"),
        ("mode", "raw", "deterministic", "dictation-span/1"),
        ("app", "ghostty", "deterministic", "dictation-span/1"),
    ]
    assert all((t["device_id"], t["start_utc_ms"], t["end_utc_ms"]) == ("nyta", T0, T0 + 6000) for t in tags)
    with pytest.raises(ValueError):
        add_tag(conn, "k", "v", "x")
    with pytest.raises(sqlite3.IntegrityError):
        add_tag(conn, "k", "v", "x", source="guess", device_id="nyta", start_utc_ms=0, end_utc_ms=1)


# ------------------------------------------------------------- ingest


@pytest.fixture
def server(tmp_path):
    cfg = make_config(tmp_path)
    srv = make_server(cfg, tokens=TOKENS)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    yield cfg, Client(host, port)
    srv.shutdown()
    srv.server_close()


def post(client: Client, body, token: str | None = "tok-oma", raw: bytes | None = None, **kw):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else json.dumps(body).encode()
    return client.request("POST", "/v1/spans", body=data, headers=headers, **kw)


def test_post_span_endpoint(server):
    cfg, client = server
    status_, payload = post(client, span_body(0, 6))
    assert status_ == 201 and payload["status"] == "created" and payload["device_id"] == "oma"
    status_, again = post(client, span_body(0, 6))
    assert status_ == 200 and again["status"] == "exists" and again["id"] == payload["id"]
    assert post(client, span_body(0, 6), token=None)[0] == 401
    assert post(client, span_body(0, 6), token="nope")[0] == 401
    assert post(client, {**span_body(1, 2), "device_id": "pi-work"})[0] == 403
    assert post(client, {**span_body(2, 3), "device_id": "oma"})[0] == 201
    status_, err = post(client, {**span_body(3, 4), "mode": "x"})
    assert status_ == 422 and err["error"].startswith("span invalid: mode")
    status_, err = post(client, None, raw=b"{not json")
    assert status_ == 422 and "invalid" in err["error"]
    assert post(client, None, raw=b"[1]")[0] == 422
    assert post(client, None, raw=b"x" * (64 * 1024 + 1))[0] == 413
    assert client.request("POST", "/v1/other", body=b"{}", headers={"Authorization": "Bearer tok-oma"})[0] == 404
    conn = dbmod.connect(cfg.db_path)
    assert conn.execute("SELECT count(*) FROM dictation_spans").fetchone()[0] == 2
    assert conn.execute("SELECT body_json FROM dictation_spans ORDER BY id LIMIT 1").fetchone()[0] == json.dumps(span_body(0, 6))
    conn.close()


# ------------------------------------------------------------- the worker


def test_span_before_audio_replaces_stt_for_its_window(tmp_path):
    """The span arrives first (the hold did its job): no STT for the covered audio, the span's
    text is the transcript, channel=dictation, lang from the span."""
    fx = Fixture(tmp_path)
    a = fx.add(T0, 10, device_id="nyta")             # fully inside the span
    b = fx.add(T0 + 20_000, 10, device_id="nyta")    # untouched
    post_span(fx.conn, "nyta", span_body(-1, 11))
    backend = FakeBackend(model_id="fake-medium")
    res = fx.worker(backend).run_once()
    assert (res.spans_applied, res.claimed, res.done, res.covered, res.failed) == (1, 2, 2, 1, 0)
    assert len(backend.calls) == 1 and len(backend.calls[0][0]) == 10 * SR  # only b was transcribed
    ca, cb = fx.chunk(a), fx.chunk(b)
    assert ca["status"] == "done" and ca["model_id"] == DICTATION_MODEL_ID
    assert cb["status"] == "done" and cb["model_id"] == "fake-medium"
    rows = fx.segments(a)
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "restart the worker" and row["lang"] == "en" and row["device_id"] == "nyta"
    assert row["model_id"] == "voxtype/parakeet-tdt-0.6b-v3-int8" and row["span_id"] is not None
    assert row["offset_ms"] == 0 and row["start_utc_ms"] == T0 - 1000
    assert tags_of(fx.conn, row["id"]) == {"channel": "dictation", "mode": "raw", "app": "ghostty"}
    assert all(r["lang"] == "no" for r in fx.segments(b))
    span = fx.conn.execute("SELECT * FROM dictation_spans").fetchone()
    assert span["status"] == "applied" and span["segment_id"] == row["id"]
    assert fx.conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                           (fts_query("restart"),)).fetchone()[0] == 1


def test_partially_covered_chunk_has_its_span_audio_zeroed(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    post_span(fx.conn, "nyta", span_body(2, 5))
    seen = {}

    def script(audio, language):
        seen["audio"] = audio
        from roomlog_server.backends import Segment, Transcript, Word
        # the backend "hears" one word in the zeroed window and one outside it
        return Transcript(segments=[
            Segment(3.0, 3.5, " ghost", [Word(3.0, 3.5, " ghost", 0.9)], avg_logprob=-0.2),
            Segment(7.0, 7.5, " hallo", [Word(7.0, 7.5, " hallo", 0.9)], avg_logprob=-0.2),
        ], language="no")

    res = fx.worker(FakeBackend(script=script)).run_once()
    assert (res.covered, res.done) == (0, 1)
    a = seen["audio"]
    assert np.all(a[2 * SR:5 * SR] == 0) and np.all(a[:2 * SR] != 0) and np.all(a[5 * SR:] != 0)
    rows = {r["text"]: r for r in fx.segments(c)}
    # inside the span the span's text stands: the leaked STT row is kept but superseded
    assert rows["ghost"]["superseded_by"] == 1 and rows["ghost"]["span_id"] is None
    assert rows["hallo"]["superseded_by"] is None and rows["restart the worker"]["span_id"] == 1
    assert fx.chunk(c)["model_id"] == "fake-model"
    assert [h["text"] for h in search(fx.conn, "ghost", fuzzy=True, include_dictation=True)] == []


def test_stt_row_crossing_a_span_edge_is_cut_at_the_edge(tmp_path):
    """Finding 5: a Whisper segment that straddles the span edge keeps its ambient words and
    only the words inside the span are superseded; without word times the row is superseded
    whole, never dropped."""
    from roomlog_server.backends import Segment, Transcript, Word
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    post_span(fx.conn, "nyta", span_body(4, 8))

    def script(audio, language):
        return Transcript(segments=[
            Segment(2.0, 5.0, " hei der inne", [Word(2.0, 2.8, " hei", 0.9), Word(3.0, 3.8, " der", 0.9),
                                                  Word(4.2, 5.0, " inne", 0.9)], avg_logprob=-0.2),
            Segment(7.5, 9.5, " ute igjen", [Word(7.5, 7.9, " ute", 0.9), Word(8.4, 9.5, " igjen", 0.9)]),
        ], language="no")

    assert fx.worker(FakeBackend(script=script)).run_once().done == 1
    rows = [(r["text"], r["start_utc_ms"] - T0, r["end_utc_ms"] - T0, r["superseded_by"])
            for r in fx.segments(c) if r["span_id"] is None]
    assert rows == [("hei der", 2000, 3800, None), ("inne", 4200, 5000, 1), ("ute", 7500, 7900, 1),
                    ("igjen", 8400, 9500, None)]
    assert [h["text"] for h in search(fx.conn, "hei", fuzzy=True)] == ["hei der"]
    assert [h["text"] for h in search(fx.conn, "igjen", fuzzy=True)] == ["igjen"]
    assert search(fx.conn, "inne", fuzzy=True, include_dictation=True) == []  # superseded: hidden, kept

    # no word times: the whole row is superseded when it overlaps, not dropped
    fx2 = Fixture(tmp_path / "b")
    c2 = fx2.add(T0, 10, device_id="nyta")
    post_span(fx2.conn, "nyta", span_body(4, 8))
    nowords = FakeBackend(script=lambda a, l: Transcript(segments=[Segment(2.0, 5.0, "hei der inne", None)],
                                                           language="no"), supports_words=False)
    assert fx2.worker(nowords).run_once().done == 1
    stt = [r for r in fx2.segments(c2) if r["span_id"] is None]
    assert len(stt) == 1 and stt[0]["text"] == "hei der inne" and stt[0]["superseded_by"] == 1


def test_late_span_supersedes_stt_rows_and_joins_the_audio(tmp_path):
    """STT already ran: its rows stay, marked superseded; the span row attaches to the chunk."""
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    w = fx.worker(FakeBackend())
    assert w.run_once().done == 1
    stt = fx.segments(c)
    assert stt and all(r["superseded_by"] is None for r in stt)
    span_id = post_span(fx.conn, "nyta", span_body(1, 4))
    res = w.run_once()
    assert (res.spans_applied, res.claimed) == (1, 0)
    rows = fx.segments(c)
    old = [r for r in rows if r["span_id"] is None]
    assert len(old) == len(stt)
    for r in old:
        overlaps = r["start_utc_ms"] < T0 + 4000 and r["end_utc_ms"] > T0 + 1000
        assert (r["superseded_by"] == span_id) == overlaps
    new = [r for r in rows if r["span_id"] == span_id]
    assert len(new) == 1 and new[0]["chunk_id"] == c and new[0]["offset_ms"] == 1000
    # requeue + retranscribe keeps the span row and the superseded marks
    fx.conn.execute("UPDATE chunks SET status = 'pending' WHERE id = ?", (c,))
    assert w.run_once().done == 1
    rows = fx.segments(c)
    assert [r for r in rows if r["span_id"] == span_id]
    assert all(r["superseded_by"] is None for r in rows if r["span_id"] is None)  # fresh STT skipped the window


def test_span_without_audio_lands_and_attaches_when_audio_arrives(tmp_path):
    fx = Fixture(tmp_path)
    w = fx.worker(FakeBackend())
    span_id = post_span(fx.conn, "nyta", span_body(0, 5))
    assert w.run_once().spans_applied == 1
    row = fx.conn.execute("SELECT * FROM segments WHERE span_id = ?", (span_id,)).fetchone()
    assert row["chunk_id"] is None and row["device_id"] == "nyta"
    hits = search(fx.conn, "restart", include_dictation=True)
    assert len(hits) == 1 and hits[0]["session_id"] is None and hits[0]["device_id"] == "nyta"
    c = fx.add(T0 + 2000, 10, device_id="nyta")
    res = w.run_once()
    assert res.done == 1 and res.covered == 0
    row = fx.conn.execute("SELECT * FROM segments WHERE span_id = ?", (span_id,)).fetchone()
    assert row["chunk_id"] == c and row["offset_ms"] == 0
    assert search(fx.conn, "restart", include_dictation=True)[0]["session_id"] == fx.chunk(c)["session_id"]


def test_cancelled_span_keeps_stt_on_the_dictation_channel(tmp_path):
    fx = Fixture(tmp_path)
    before = fx.add(T0, 10, device_id="nyta")
    w = fx.worker(FakeBackend())
    assert w.run_once().done == 1
    post_span(fx.conn, "nyta", span_body(2, 4, text="", cancelled=True))
    late = fx.add(T0 + 3000, 5, device_id="nyta")
    res = w.run_once()
    assert (res.spans_applied, res.done, res.covered) == (1, 1, 0)
    assert fx.conn.execute("SELECT count(*) FROM segments WHERE span_id IS NOT NULL").fetchone()[0] == 0
    for cid in (before, late):
        rows = fx.segments(cid)
        assert rows and all(r["superseded_by"] is None for r in rows)
        for r in rows:
            inside = r["start_utc_ms"] < T0 + 4000 and r["end_utc_ms"] > T0 + 2000
            t = tags_of(fx.conn, r["id"])
            assert (t.get("channel") == "dictation" and t.get("cancelled") == "true") == inside, (cid, r, t)
    assert search(fx.conn, "ord", fuzzy=True) == []  # every STT row overlaps the span: nothing ambient left
    assert len(search(fx.conn, "ord", fuzzy=True, include_dictation=True)) == len(fx.segments(before)) + len(fx.segments(late))


def test_span_row_survives_its_chunk_being_deleted_and_rejoins_new_audio(tmp_path):
    """Finding 4: `roomlog resegment` deletes derived chunks; the span row is detached, not
    cascaded away, and joins the re-derived chunk."""
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    span_id = post_span(fx.conn, "nyta", span_body(1, 4))
    w = fx.worker(FakeBackend())
    assert w.run_once().done == 1
    row = fx.conn.execute("SELECT * FROM segments WHERE span_id = ?", (span_id,)).fetchone()
    assert row["chunk_id"] == c
    fx.conn.execute("DELETE FROM chunks WHERE id = ?", (c,))  # what reset_segmentation does
    rows = fx.conn.execute("SELECT * FROM segments").fetchall()
    assert [(r["span_id"], r["chunk_id"], r["text"]) for r in rows] == [(span_id, None, "restart the worker")]
    assert fx.conn.execute("SELECT segment_id FROM dictation_spans WHERE id = ?", (span_id,)).fetchone()[0] == row["id"]
    assert search(fx.conn, "restart", include_dictation=True)[0]["session_id"] is None
    c2 = fx.add(T0, 10, device_id="nyta")
    assert w.run_once().done == 1
    row = fx.conn.execute("SELECT * FROM segments WHERE span_id = ?", (span_id,)).fetchone()
    assert row["chunk_id"] == c2 and row["offset_ms"] == 1000
    assert all(r["superseded_by"] is None for r in fx.segments(c2) if r["span_id"] is None)


def test_cancelled_span_is_clamped_to_ten_minutes():
    """Finding 2: a client that never saw the end guesses; the server bounds the guess."""
    s = validate_span(span_body(0, 1800, text="", cancelled=True))
    assert s["cancelled"] and s["end_utc_ms"] - s["start_utc_ms"] == 600_000
    s = validate_span(span_body(0, 1800, text="a real half hour"))
    assert not s["cancelled"] and s["end_utc_ms"] - s["start_utc_ms"] == 1_800_000


def test_hold_delays_fresh_chunks_until_a_span_can_arrive(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    backend = FakeBackend()
    clock = {"now": T0 + 10_000 + 5_000}  # 5 s after the chunk ended, hold is 20 s
    w = fx.worker(backend, now=lambda: clock["now"])
    assert w.run_once().claimed == 0
    post_span(fx.conn, "nyta", span_body(0, 10))
    clock["now"] = T0 + 10_000 + 20_000
    res = w.run_once()
    assert (res.claimed, res.covered) == (1, 1) and backend.calls == []


# ------------------------------------------------------------- queries, CLI, MCP


def seed_mixed(tmp_path):
    fx = Fixture(tmp_path)
    c = fx.add(T0, 10, device_id="nyta")
    post_span(fx.conn, "nyta", span_body(2, 4, text="open the budget note"))
    w = fx.worker(FakeBackend())
    w.run_once()
    fx.conn.execute("UPDATE segments SET text = 'budsjettet er klart' WHERE span_id IS NULL AND idx = 0")
    return fx, c


def test_default_queries_hide_dictation_and_include_flag_shows_it(tmp_path):
    fx, c = seed_mixed(tmp_path)
    assert [h["text"] for h in search(fx.conn, "budget")] == []
    hits = search(fx.conn, "budget", include_dictation=True)
    assert [(h["text"], h["lang"], h["channel"]) for h in hits] == [("open the budget note", "en", "dictation")]
    assert hits[0]["span_id"] == 1
    amb = search(fx.conn, "budsjettet")
    assert amb and amb[0]["channel"] == "ambient" and amb[0]["lang"] == "no"
    sid = fx.chunk(c)["session_id"]
    assert all(s["channel"] == "ambient" for s in session_segments(fx.conn, sid))
    both = session_segments(fx.conn, sid, include_dictation=True)
    assert [s["channel"] for s in both].count("dictation") == 1
    lines = [l for l in __import__("roomlog_server.queries", fromlist=["transcript_lines"]).transcript_lines(both)
             if "dictation" in l]
    assert lines == ["[10:00:02] (dictation en) open the budget note"]
    ctx = get_segment_context(fx.conn, hits[0]["segment_id"], window_s=60)
    assert ctx["segment"]["channel"] == "dictation"
    assert all(x["channel"] == "ambient" for x in ctx["context"])
    assert any(x["channel"] == "dictation" for x in
               get_segment_context(fx.conn, hits[0]["segment_id"], 60, include_dictation=True)["context"])
    assert get_session(fx.conn, sid)["n_segments"] == len(both)  # sessions count non-superseded rows
    st = status(fx.conn)
    assert st["spans"] == {"pending": 0, "applied": 1, "superseded_segments": 0}
    spans = list_spans(fx.conn, device_id="nyta")
    assert len(spans) == 1 and spans[0]["status"] == "applied" and spans[0]["cancelled"] is False
    assert spans[0]["start_utc"] == ms_to_iso(T0 + 2000) and "body_json" not in spans[0]
    assert list_spans(fx.conn, device_id="other") == []


def test_channel_is_the_latest_deterministic_tag_and_the_filter_agrees(tmp_path):
    """Finding 6: one rule. A later model tag does not override a deterministic one; a
    model-only tag counts; a row whose deterministic channel says ambient is listed even if
    an older tag said dictation."""
    fx, c = seed_mixed(tmp_path)
    amb = search(fx.conn, "budsjettet")[0]
    add_tag(fx.conn, "channel", "dictation", "classifier/0.1", source="model", segment_id=amb["segment_id"])
    assert search(fx.conn, "budsjettet") == []  # a model tag alone decides
    assert search(fx.conn, "budsjettet", include_dictation=True)[0]["channel"] == "dictation"
    add_tag(fx.conn, "channel", "ambient", "dictation-span/2", segment_id=amb["segment_id"])
    assert search(fx.conn, "budsjettet")[0]["channel"] == "ambient"  # deterministic wins over the model
    add_tag(fx.conn, "channel", "dictation", "classifier/0.2", source="model", segment_id=amb["segment_id"])
    assert search(fx.conn, "budsjettet")[0]["channel"] == "ambient"  # a later model tag changes nothing
    dic = search(fx.conn, "budget", include_dictation=True)[0]
    add_tag(fx.conn, "channel", "ambient", "operator/1", segment_id=dic["segment_id"])
    assert search(fx.conn, "budget")[0]["channel"] == "ambient"  # latest deterministic tag wins
    sid = fx.chunk(c)["session_id"]
    assert {s["channel"] for s in session_segments(fx.conn, sid)} == {"ambient"}
    assert len(session_segments(fx.conn, sid)) == len(session_segments(fx.conn, sid, include_dictation=True))


def test_cli_and_mcp_expose_lang_channel_and_the_include_flag(tmp_path, capsys):
    fx, c = seed_mixed(tmp_path)
    toml = fx.cfg.config_dir / "server.toml"
    toml.write_text(f'[paths]\ndata_dir = "{fx.cfg.data_dir}"\n')
    base = ["-c", str(toml)]
    assert main(base + ["search", "budget"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert main(base + ["search", "budget", "--include-dictation"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("  en dictation  open the budget note\n") and out.startswith("nyta_")
    main(base + ["search", "budsjettet"])
    assert "  no ambient  budsjettet er klart" in capsys.readouterr().out
    sid = fx.chunk(c)["session_id"]
    main(base + ["session", sid])
    assert "dictation" not in capsys.readouterr().out
    main(base + ["session", sid, "--include-dictation"])
    assert "[10:00:02] (dictation en) open the budget note" in capsys.readouterr().out
    assert main(base + ["spans", "--device", "nyta"]) == 0
    out = capsys.readouterr().out
    assert "nyta" in out and "en raw" in out and "applied" in out and "open the budget note" in out
    main(base + ["--json", "spans"])
    assert json.loads(capsys.readouterr().out)[0]["mode"] == "raw"
    main(base + ["status"])
    assert "spans" in capsys.readouterr().out

    t = Tools(fx.cfg)
    assert t.search("budget") == []
    assert t.search("budget", include_dictation=True)[0]["channel"] == "dictation"
    assert "dictation" not in t.get_session(sid)["transcript"]
    assert "(dictation en)" in t.get_session(sid, include_dictation=True)["transcript"]
    assert t.list_spans(device_id="nyta")[0]["text"] == "open the budget note"
    from roomlog_server.mcp_server import build_server
    import asyncio
    tools = asyncio.run(build_server(fx.cfg).list_tools())
    names = {x.name for x in tools}
    assert "list_spans" in names
    search_tool = next(x for x in tools if x.name == "search")
    props = getattr(search_tool, "input_schema", getattr(search_tool, "inputSchema", None))["properties"]
    assert "include_dictation" in props


# ------------------------------------------------------------- migration


def test_migration_keeps_the_segment_id_counter(tmp_path):
    """Finding 7: the v4 rebuild must not let a deleted row's id come back."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    dbmod.migrate(conn, target=3)
    body = b"y" * 10
    meta = make_sidecar(body, device_id="lass22", start_utc=ms_to_iso(T0), duration_s=10)
    cid = insert_chunk_row(conn, meta, json.dumps(meta), hashlib.sha256(body).hexdigest(), "2026/09/26/y.opus")
    for i in range(3):
        conn.execute("INSERT INTO segments (chunk_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang, model_id) "
                     "VALUES (?, ?, ?, ?, 0, ?, 'no', 'm1')", (cid, i, T0 + i * 1000, T0 + i * 1000 + 1000, f"rad {i}"))
    conn.execute("DELETE FROM segments WHERE id = 3")
    conn.close()
    conn = dbmod.connect(path)
    conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) "
                 "VALUES (?, 'lass22', 3, 0, 1, 0, 'ny rad', 'm1')", (cid,))
    assert conn.execute("SELECT max(id) FROM segments").fetchone()[0] == 4
    conn.close()


def test_migration_v3_to_v4_keeps_rows_and_backfills_lang(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    dbmod.migrate(conn, target=3)
    body = b"x" * 10
    meta = make_sidecar(body, device_id="lass22", start_utc=ms_to_iso(T0), duration_s=10)
    cid = insert_chunk_row(conn, meta, json.dumps(meta), hashlib.sha256(body).hexdigest(), "2026/09/26/x.opus")
    conn.execute("INSERT INTO segments (chunk_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang, model_id) "
                 "VALUES (?, 0, ?, ?, 0, 'gammel rad', NULL, 'm1')", (cid, T0, T0 + 1000))
    conn.execute("INSERT INTO segments (chunk_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang, model_id) "
                 "VALUES (?, 1, ?, ?, 0, 'engelsk rad', 'en', 'm1')", (cid, T0 + 1000, T0 + 2000))
    conn.execute("INSERT INTO sessions (id, device_id, gap_s, start_utc_ms, end_utc_ms, n_chunks, n_segments) "
                 "VALUES ('s1', 'lass22', 300, 0, 1, 1, 2)")
    conn.execute("UPDATE chunks SET session_id = 's1', status = 'done' WHERE id = ?", (cid,))
    conn.close()

    conn = dbmod.connect(path)
    assert dbmod.user_version(conn) == 5
    rows = conn.execute("SELECT * FROM segments ORDER BY id").fetchall()
    assert [(r["text"], r["lang"], r["device_id"], r["chunk_id"], r["span_id"], r["superseded_by"]) for r in rows] == [
        ("gammel rad", "no", "lass22", cid, None, None), ("engelsk rad", "en", "lass22", cid, None, None)]
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("gammel"),)).fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute("INSERT INTO segments (chunk_id, device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) "
                 "VALUES (?, 'lass22', 2, 0, 1, 0, 'nyaste rad', 'm1')", (cid,))
    new_id = conn.execute("SELECT max(id) FROM segments").fetchone()[0]
    assert new_id > rows[-1]["id"]
    assert conn.execute("SELECT lang FROM segments WHERE id = ?", (new_id,)).fetchone()[0] == "no"
    assert conn.execute("SELECT count(*) FROM segments_tri WHERE segments_tri MATCH ?",
                        (fts_query("nyaste"),)).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):  # the lang column is NOT NULL now
        conn.execute("INSERT INTO segments (device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, lang, model_id) "
                     "VALUES ('x', 0, 0, 1, 0, 't', NULL, 'm')")
    # a chunkless row is allowed (a span with no audio yet), and the cascade still works
    conn.execute("INSERT INTO segments (device_id, idx, start_utc_ms, end_utc_ms, offset_ms, text, model_id) "
                 "VALUES ('nyta', 0, 0, 1, 0, 'uten lyd', 'dict')")
    conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
    assert [r[0] for r in conn.execute("SELECT text FROM segments")] == ["uten lyd"]
    assert conn.execute("SELECT count(*) FROM segments_fts WHERE segments_fts MATCH ?",
                        (fts_query("gammel"),)).fetchone()[0] == 0
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"dictation_spans", "tags"} <= names
    conn.close()
