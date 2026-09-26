# roomlog-edge

Always-on room audio capture for roomlog: mic → sample-counter timeline → Silero VAD →
speech chunks → Ogg Opus + JSON sidecar → spool → `PUT /v1/chunks/{sha256}`.

Design: `~/hvelv/repos/roomlog/spec-v1-design.md` §3; wire contract: `../contract/`.

```
uv sync                      # Python 3.12 (pinned: onnxruntime wheels), numpy, sounddevice, onnxruntime
uv run pytest -q             # no model, no mic, no network; ffmpeg or opusenc for the encoder tests
uv run roomlog-edge --help   # capture | upload | health | devices | selftest
```

Modules (`src/roomlog_edge/`):

| module | role |
|---|---|
| `timeline.py` | pure: sample counter anchored to UTC; epochs, xrun/silent-loss detection, clock steps, drift anchors |
| `chunker.py` | pure: state machine over (window index, probability) → non-overlapping sample ranges |
| `vad.py` | Silero VAD v6.2.3 through onnxruntime (lazy import); `FunctionVad` for tests |
| `capture.py` | `Pipeline` (ring buffer, windowing, encode worker, stamping, unsynced hold) and `run_capture` (PortAudio, sync gate, watchdog) |
| `encode.py` | `opusenc` or `ffmpeg -c:a libopus` subprocess with the `ROOMLOG_*` tags |
| `spool.py` | `tmp/ pending/ unsynced/ failed/`, temp+fsync+rename, disk guard |
| `uploader.py` | stdlib `http.client` PUT loop, status handling, backoff with full jitter |
| `health.py` | status files + spool + NTP → `.prom` file, optional healthchecks ping |
| `clocksync.py`, `sdnotify.py`, `status.py`, `sidecar.py`, `ogg.py`, `config.py`, `cli.py` | small helpers |

Install: `../deploy/edge/install.sh --mode user|system [--dry-run]`; config example in
`../deploy/edge/edge.toml.example`. The installer fetches `silero_vad.onnx` and pins its
SHA-256 into the config; it never enables a unit.
