"""``roomlog-edge`` command line: capture, upload, health, devices, selftest."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading

from . import __version__, config as configmod


def _load(args) -> configmod.Config:
    return configmod.load(args.config or configmod.default_config_path())


def cmd_capture(args) -> int:
    from .capture import run_capture

    return run_capture(_load(args))


def cmd_upload(args) -> int:
    from .spool import Spool
    from .uploader import Client, Uploader

    cfg = _load(args)
    spool = Spool(cfg.spool_dir, cfg.spool.max_bytes, cfg.spool.min_free_fraction)
    client = Client(cfg.server_url, cfg.read_token(), cfg.uploader.timeout_s)
    up = Uploader(spool, client, cfg.uploader.max_backoff_s, cfg.uploader.idle_poll_s, cfg.uploader_status)
    if args.once:
        outcomes = up.run_once()
        up.write_status()
        for o in outcomes:
            print(f"{o.action:5s} {o.status} {o.detail[:80]}")
        return 0 if all(o.action == "ack" for o in outcomes) else 1
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    up.run_forever(stop)
    return 0


def cmd_health(args) -> int:
    from .health import run_health

    return run_health(_load(args))


def cmd_devices(args) -> int:
    try:
        import sounddevice as sd
    except OSError as e:
        print(f"sounddevice unavailable (libportaudio?): {e}", file=sys.stderr)
        return 1
    print(sd.query_devices())
    try:
        default = sd.query_devices(kind="input")
        print(f"\ndefault input: [{default['index']}] {default['name']}")
    except Exception as e:  # noqa: BLE001
        print(f"\nno default input device: {e}")
    return 0


def cmd_selftest(args) -> int:
    import time

    import numpy as np

    from . import clocksync, vad as vadmod
    from .encode import Encoder, EncoderError, roomlog_tags
    from .ogg import parse
    from .uploader import Client

    cfg = _load(args)
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {name}{': ' + detail if detail else ''}")

    # model
    try:
        vad = vadmod.SileroVad(cfg.model_path, cfg.audio.sample_rate, cfg.model_sha256)
        report("model loads", True, str(cfg.model_path))
        t = np.arange(vadmod.WINDOW * 20) / cfg.audio.sample_rate
        tone = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + 0.5 * np.sin(2 * np.pi * 4 * t))).astype(np.float32)
        silence = np.zeros_like(tone)
        t0 = time.perf_counter()
        p_tone = max(vad(tone[i:i + vadmod.WINDOW]) for i in range(0, len(tone), vadmod.WINDOW))
        vad.reset()
        p_sil = max(vad(silence[i:i + vadmod.WINDOW]) for i in range(0, len(silence), vadmod.WINDOW))
        per_window_ms = (time.perf_counter() - t0) / 40 * 1000
        report("VAD runs", True, f"tone burst max p={p_tone:.2f}, silence max p={p_sil:.2f}, {per_window_ms:.2f} ms/window")
        report("VAD silence below threshold", p_sil < cfg.vad.threshold)
    except Exception as e:  # noqa: BLE001
        report("model loads", False, str(e))

    # encoder
    try:
        enc = Encoder(cfg.encoder.backend, cfg.encoder.bitrate_kbps, cfg.audio.sample_rate)
        pcm = (np.sin(np.arange(16000) / 16000 * 2 * np.pi * 440) * 8000).astype(np.int16).tobytes()
        out = enc.encode(pcm, roomlog_tags(cfg.device_id, "selftest", 0, 0, __version__))
        info = parse(out)
        report("encoder", info.tags.get("ROOMLOG_DEVICE_ID") == cfg.device_id,
               f"{enc.backend}, {len(out)} bytes for 1 s, {info.duration_s:.2f} s decoded")
    except (EncoderError, ValueError) as e:
        report("encoder", False, str(e))

    # clock
    state = clocksync.ntp_synchronized()
    report("clock synced", bool(state), {True: "yes", False: "no", None: "unknown (timedatectl missing)"}[state])

    # spool
    report("spool dir writable", cfg.spool_dir.exists() or _mkdir_ok(cfg.spool_dir), str(cfg.spool_dir))

    # server
    try:
        client = Client(cfg.server_url, cfg.read_token(), cfg.uploader.timeout_s)
        status, body = client.whoami()
        dev = json.loads(body).get("device_id") if status == 200 else None
        report("GET /v1/whoami", status == 200 and dev == cfg.device_id, f"{status} {body[:100]!r}")
    except Exception as e:  # noqa: BLE001
        report("GET /v1/whoami", False, str(e))

    return 0 if ok else 1


def _mkdir_ok(path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roomlog-edge", description="roomlog edge client")
    p.add_argument("--version", action="version", version=f"roomlog-edge {__version__}")
    p.add_argument("-c", "--config", help="path to edge.toml (default: $ROOMLOG_EDGE_CONFIG or ~/.config/roomlog-edge/edge.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("capture", help="mic → VAD → Opus chunks in the spool").set_defaults(fn=cmd_capture)
    up = sub.add_parser("upload", help="spool/pending → server")
    up.add_argument("--once", action="store_true", help="one pass, then exit")
    up.set_defaults(fn=cmd_upload)
    sub.add_parser("health", help="write metrics file, exit 1 when unhealthy").set_defaults(fn=cmd_health)
    sub.add_parser("devices", help="list audio input devices").set_defaults(fn=cmd_devices)
    sub.add_parser("selftest", help="model, VAD, encoder, clock and server checks").set_defaults(fn=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except configmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
