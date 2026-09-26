"""`roomlog` command line (§4.6)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from . import db as dbmod
from .config import Config, load_config
from .times import parse_user_time


def _add_time_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="from_utc", help="start (date, ISO 8601 or epoch ms)")
    p.add_argument("--to", dest="to_utc", help="end, exclusive")
    p.add_argument("--device", dest="device_id", help="only this device_id")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roomlog", description="roomlog server: ingest, worker, search, sessions.")
    p.add_argument("-c", "--config", help="server.toml (default ~/.config/roomlog/server.toml or $ROOMLOG_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true", help="machine-readable output where it applies")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="run the ingest HTTP server")
    s.add_argument("--bind", help="host:port (default from config / $ROOMLOG_BIND)")

    s = sub.add_parser("worker", help="run the transcription worker")
    s.add_argument("--once", action="store_true", help="process one batch and exit")

    s = sub.add_parser("search", help="full-text search over segments")
    s.add_argument("query")
    _add_time_filters(s)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--fuzzy", action="store_true", help="trigram substring match")

    s = sub.add_parser("sessions", help="list sessions")
    _add_time_filters(s)
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("session", help="print one session's transcript")
    s.add_argument("session_id")

    s = sub.add_parser("export", help="write one markdown file per session")
    s.add_argument("--vault", required=True, help="target directory")
    _add_time_filters(s)
    s.add_argument("--closed-only", action="store_true")

    s = sub.add_parser("status", help="counts and per-device state")
    s.add_argument("--prom", action="store_true", help="Prometheus text format")
    s.add_argument("--write", metavar="PATH", help="write output to PATH (atomic); default with --prom: none")

    sub.add_parser("verify", help="re-hash the archive against sidecars and the DB")
    sub.add_parser("sessionize", help="rebuild sessions from chunks")

    s = sub.add_parser("requeue", help="put failed (and attempt-exhausted) chunks back in the queue")
    s.add_argument("--failed", action="store_true", help="only rows with status=failed")
    s.add_argument("--device", dest="device_id")
    s.add_argument("--since", dest="since_utc", help="only chunks starting at or after this time")

    s = sub.add_parser("segment", help="one segmenter pass: pending raw segments → derived chunks")

    s = sub.add_parser("resegment", help="undo segmentation for a device's raw segments and re-run it")
    s.add_argument("--device", dest="device_id", required=True)
    s.add_argument("--from", dest="from_utc", help="raw segments starting at or after this time")
    s.add_argument("--to", dest="to_utc", help="raw segments starting before this time")

    sub.add_parser("purge-raw", help="delete speech-free raw segments older than the retention (30 days)")

    s = sub.add_parser("fetch-model", help="download the local backend's ct2 model and the Silero VAD")
    s.add_argument("--model", help="HF repo id (default: first local backend's model)")
    s.add_argument("--revision")
    s.add_argument("--vad-only", action="store_true", help="only the Silero model")

    s = sub.add_parser("selftest", help="check config, DB, archive, tokens and every backend")
    s.add_argument("--no-transcribe", action="store_true", help="probe only")
    return p


def _ro(cfg: Config):
    assert cfg.db_path is not None
    if not Path(cfg.db_path).exists():
        raise SystemExit(f"no database at {cfg.db_path}; nothing ingested yet")
    return dbmod.connect(cfg.db_path, readonly=True)


def _out(args: argparse.Namespace, payload: Any, text: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text, end="" if text.endswith("\n") else "\n")


def cmd_ingest(cfg: Config, args: argparse.Namespace) -> int:
    from .ingest import serve
    serve(cfg, bind=args.bind)
    return 0


def cmd_worker(cfg: Config, args: argparse.Namespace) -> int:
    from .backends import Router, build_backends
    from .segmenter import segmenter_from_config
    from .worker import Worker
    assert cfg.db_path is not None
    conn = dbmod.connect(cfg.db_path)
    w = Worker(cfg, conn, Router(build_backends(cfg)), segmenter=segmenter_from_config(cfg, conn))
    if args.once:
        res = w.run_once()
        print(f"claimed={res.claimed} done={res.done} failed={res.failed} windows={res.windows} "
              f"segmented={res.segmented_chunks}")
        for e in res.errors:
            print(f"  {e}")
        return 0 if not res.errors else 1
    w.run_forever()
    return 0


def _segment(cfg: Config, conn, args: argparse.Namespace) -> int:
    from .segmenter import segmenter_from_config
    res = segmenter_from_config(cfg, conn).run_once()
    print(f"epochs={res.epochs} chunks={res.chunks}")
    for e in res.errors:
        print(f"  {e}")
    return 0 if not res.errors else 1


def cmd_segment(cfg: Config, args: argparse.Namespace) -> int:
    assert cfg.db_path is not None
    return _segment(cfg, dbmod.connect(cfg.db_path), args)


def cmd_resegment(cfg: Config, args: argparse.Namespace) -> int:
    from .segmenter import reset_segmentation
    assert cfg.db_path is not None
    conn = dbmod.connect(cfg.db_path)
    out = reset_segmentation(conn, args.device_id, parse_user_time(args.from_utc), parse_user_time(args.to_utc))
    print(f"reset epochs={out['epochs']} raw={out['raw_reset']} chunks_deleted={out['chunks_deleted']}")
    return _segment(cfg, conn, args)


def cmd_purge_raw(cfg: Config, args: argparse.Namespace) -> int:
    from .segmenter import RAW_RETENTION_DAYS, purge_raw
    assert cfg.db_path is not None and cfg.archive_dir is not None
    conn = dbmod.connect(cfg.db_path)
    out = purge_raw(conn, cfg.archive_dir)
    print(f"deleted={out['deleted']} bytes={out['bytes']} retention_days={RAW_RETENTION_DAYS}")
    return 0


def cmd_search(cfg: Config, args: argparse.Namespace) -> int:
    from .queries import search
    conn = _ro(cfg)
    rows = search(conn, args.query, parse_user_time(args.from_utc), parse_user_time(args.to_utc),
                  args.device_id, args.limit, args.offset, args.fuzzy)
    text = "".join(f"{r['session_id'] or '-'}  {r['start_utc']}  {r['text']}\n" for r in rows)
    _out(args, rows, text)
    return 0


def cmd_sessions(cfg: Config, args: argparse.Namespace) -> int:
    from .queries import list_sessions
    conn = _ro(cfg)
    rows = list_sessions(conn, parse_user_time(args.from_utc), parse_user_time(args.to_utc),
                         args.device_id, args.limit)
    text = "".join(
        f"{r['session_id']}  {r['start_utc']}  {r['duration_s']:>8.1f}s  chunks={r['n_chunks']} "
        f"segments={r['n_segments']}  {'closed' if r['closed'] else 'open'}\n"
        for r in rows
    )
    _out(args, rows, text)
    return 0


def cmd_session(cfg: Config, args: argparse.Namespace) -> int:
    from .queries import get_session, transcript_lines
    conn = _ro(cfg)
    s = get_session(conn, args.session_id)
    if s is None:
        print(f"no session {args.session_id}", file=sys.stderr)
        return 1
    head = (f"{s['session_id']}  {s['device_id']}  {s['start_utc']} → {s['end_utc']}  "
            f"chunks={s['n_chunks']} segments={s['n_segments']}\n")
    _out(args, s, head + "\n".join(transcript_lines(s["segments"])) + "\n")
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    from .export import export_sessions
    conn = _ro(cfg)
    res = export_sessions(conn, Path(args.vault).expanduser(), parse_user_time(args.from_utc),
                          parse_user_time(args.to_utc), args.device_id, args.closed_only)
    _out(args, res, f"written={res['written']} unchanged={res['unchanged']}\n")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .queries import status
    from .status import render_prom, render_text, write_atomic
    conn = _ro(cfg)
    st = status(conn, cfg.archive_dir)
    text = render_prom(st) if args.prom else render_text(st)
    if args.write:
        write_atomic(Path(args.write).expanduser(), text)
        return 0
    if args.json and not args.prom:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print(text, end="")
    return 0


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    from .verify import verify_archive
    conn = _ro(cfg)
    assert cfg.archive_dir is not None
    res = verify_archive(conn, cfg.archive_dir)
    text = f"files={res['files']} rows={res['rows']} problems={len(res['problems'])} derived={res['derived']}\n"
    text += "".join(f"  {p}\n" for p in res["problems"])
    _out(args, res, text)
    return 0 if res["ok"] else 1


def cmd_sessionize(cfg: Config, args: argparse.Namespace) -> int:
    from .sessions import rebuild_sessions
    assert cfg.db_path is not None
    conn = dbmod.connect(cfg.db_path)
    n = rebuild_sessions(conn, cfg.session_gap_s)
    print(f"sessions={n}")
    return 0


def cmd_requeue(cfg: Config, args: argparse.Namespace) -> int:
    from .worker import requeue
    assert cfg.db_path is not None
    conn = dbmod.connect(cfg.db_path)
    n = requeue(conn, cfg.max_attempts, failed_only=args.failed, device_id=args.device_id,
                since_ms=parse_user_time(args.since_utc))
    print(f"requeued={n}")
    return 0


def cmd_fetch_model(cfg: Config, args: argparse.Namespace) -> int:
    from .models import fetch_model, fetch_vad_model
    vad_path = fetch_vad_model(cfg)
    print(f"vad model ready at {vad_path}")
    if args.vad_only:
        return 0
    path = fetch_model(cfg, args.model, args.revision)
    print(f"model ready at {path}")
    return 0


def cmd_selftest(cfg: Config, args: argparse.Namespace) -> int:
    from .selftest import run_selftest
    rep = run_selftest(cfg, transcribe=not args.no_transcribe)
    text = "".join(f"[{'ok' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}\n" for c in rep["checks"])
    _out(args, rep, text)
    return 0 if rep["ok"] else 1


COMMANDS = {
    "ingest": cmd_ingest,
    "worker": cmd_worker,
    "search": cmd_search,
    "sessions": cmd_sessions,
    "session": cmd_session,
    "export": cmd_export,
    "status": cmd_status,
    "verify": cmd_verify,
    "sessionize": cmd_sessionize,
    "requeue": cmd_requeue,
    "segment": cmd_segment,
    "resegment": cmd_resegment,
    "purge-raw": cmd_purge_raw,
    "fetch-model": cmd_fetch_model,
    "selftest": cmd_selftest,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    cfg = load_config(args.config)
    return COMMANDS[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
