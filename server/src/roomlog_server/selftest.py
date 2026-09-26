"""`roomlog selftest`: config, DB, archive, tokens, every backend's probe, one transcription.

The sample is the newest chunk in the archive when there is one, else a synthetic tone,
so the path is exercised without shipping audio in the package.
"""

from __future__ import annotations

import time
from typing import Any

from . import db as dbmod
from .audio import decode_opus, synthetic_tone
from .backends import build_backends
from .config import Config, load_tokens


def run_selftest(cfg: Config, transcribe: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {"ok": True, "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False

    assert cfg.db_path and cfg.archive_dir and cfg.tokens_file
    try:
        conn = dbmod.connect(cfg.db_path)
        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        check("db", True, f"{cfg.db_path} schema v{dbmod.user_version(conn)}, {n} chunks")
    except Exception as e:
        conn = None
        check("db", False, str(e))
    try:
        cfg.archive_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.archive_dir / ".selftest"
        probe.write_text("ok")
        probe.unlink()
        check("archive writable", True, str(cfg.archive_dir))
    except Exception as e:
        check("archive writable", False, str(e))
    try:
        tokens = load_tokens(cfg.tokens_file)
        check("tokens", bool(tokens), f"{len(tokens)} device token(s) in {cfg.tokens_file}")
    except Exception as e:
        check("tokens", False, str(e))

    if cfg.vad_backend == "energy":
        check("vad model", True, "energy gate configured; Silero not needed")
    else:
        model = cfg.vad_model_path
        assert model is not None
        raw_pending = 0
        if conn is not None:
            raw_pending = conn.execute("SELECT count(*) FROM raw_segments WHERE status = 'pending'").fetchone()[0]
        if model.exists():
            check("vad model", True, str(model))
        else:
            # Only raw clients need it; a Pi-only server is fine without.
            check("vad model", raw_pending == 0,
                  f"{model} missing ({raw_pending} raw segments waiting); run `roomlog fetch-model`")

    sample = None
    if conn is not None:
        row = conn.execute("SELECT path FROM chunks ORDER BY received_utc_ms DESC LIMIT 1").fetchone()
        if row is not None and (cfg.archive_dir / row["path"]).exists():
            try:
                sample = decode_opus(cfg.archive_dir / row["path"])
                check("decode", True, f"{row['path']} → {len(sample) / 16000:.2f} s")
            except Exception as e:
                check("decode", False, f"{row['path']}: {e}")
    if sample is None:
        sample = synthetic_tone(2.0)

    backends = build_backends(cfg)
    check("backends configured", bool(backends), ", ".join(f"{b.name}({b.__class__.__name__})" for b in backends))
    for b in backends:
        try:
            ok = b.probe()
        except Exception as e:
            ok = False
            check(f"probe {b.name}", False, str(e))
            continue
        check(f"probe {b.name}", ok, b.model_id)
        if ok and transcribe:
            t0 = time.monotonic()
            try:
                t = b.transcribe(sample, cfg.language)
                dt = time.monotonic() - t0
                text = " ".join(s.text.strip() for s in t.segments)[:120]
                check(f"transcribe {b.name}", True,
                      f"{dt:.1f} s for {len(sample) / 16000:.1f} s audio, words={t.has_words}: {text!r}")
            except Exception as e:
                check(f"transcribe {b.name}", False, str(e))
    if conn is not None:
        conn.close()
    return report
