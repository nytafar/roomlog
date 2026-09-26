# roomlog-server

Ingest, archive, transcription worker, sessions, CLI (`roomlog`) and MCP server (`roomlog-mcp`).
Design: `~/hvelv/repos/roomlog/spec-v1-design.md`; raw segments and server-side segmentation:
ADR 0005, `~/hvelv/repos/roomlog/android/plan-thin-client.md`. Gate: `uv run pytest -q` in
this directory (no model weights, no network; ffmpeg encodes the test audio).

## Layout

| Module | What |
|---|---|
| `config.py` | `server.toml` + `tokens.toml`; every path comes from here |
| `db.py` | SQLite schema, WAL, `PRAGMA user_version` migrations (v3: `raw_segments`, `raw_progress`, `chunks.kind`), FTS5 (unicode61 + trigram) |
| `ingest.py`, `archive.py`, `sidecar.py` | `PUT /v1/chunks/{sha}`, `GET /v1/time` per `contract/CONTRACT.md`; temp+fsync+rename; `kind: raw` lands under `archive/raw/` |
| `chunker.py`, `vad.py` | copies of the edge modules (Silero through onnxruntime, the speech chunker); tests verbatim |
| `segmenter.py` | raw segments → derived chunks per `(device, run_id, epoch)`; `reset_segmentation`, `purge_raw` |
| `backends/` | `local` (faster-whisper, lazy), `openai` (Berget and friends), `whisper_cpp`, `fake`; `Router` = first healthy, failover on connection error / timeout / 5xx |
| `worker.py` | segment raw → claim → `audio_for` (file or raw slice) → window (≤ 30 s, 0.3 s gaps) → transcribe → map words to chunks → filter → write → sessionize; purges raw once a day |
| `sessions.py` | gap-and-island per device, deterministic ids, full rebuild in one transaction |
| `queries.py` | read-only queries shared by the CLI and MCP tools |
| `cli.py`, `mcp_server.py` | `roomlog …`, `roomlog-mcp` (stdio) |

## Chunk kinds

`chunks.kind` is `speech` (a file the Linux edge uploaded) or `derived` (no file: a slice
`[n_start, n_start + n_samples)` of the raw stream of one `(device, run_id, epoch)`, identity
= SHA-256 of that int16 PCM slice). Both kinds flow through the same worker, sessions, search
and MCP. Raw segments keep `status` `pending` until the segmenter has covered them;
`raw_progress.segmented_to_n` is the per-epoch high-water mark. A raw file that does not
decode to its `n_samples` (beyond 64 samples of codec priming) gets `status` `failed`, is
counted in `roomlog status`, and its neighbours continue as if it were a gap; `resegment`
over its time puts it back to `pending` for another try.

`[segmenter] vad = "energy"` is an RMS gate for tests and for bootstrapping before
`fetch-model`; it is not for production. The 30-day `purge-raw` deletes raw audio that
overlaps no derived chunk, so with the energy gate it would delete on the gate's decisions.
Existing `server.toml` files without a `[segmenter]` section get the defaults (`silero`,
`raw_idle_s = 120`, thresholds 0.5 / 0.35); see `deploy/server/server.toml.example`.

## Run

```
uv sync
uv run roomlog --help
uv run roomlog -c ~/.config/roomlog/server.toml status
uv run roomlog fetch-model            # NB-Whisper ct2 snapshot and the pinned Silero blob
uv run roomlog segment                # one segmenter pass (the worker does this every poll)
uv run roomlog resegment --device s22 --from 2026-09-26   # re-run VAD + chunker over history
uv run roomlog resegment --device s22 --from 2026-09-26T13:00 --to 2026-09-26T13:30  # only that range; chunks outside stay
uv run roomlog purge-raw              # 30-day retention for speech-free raw audio (the worker: daily)
uv run roomlog verify                 # archive, archive/raw and derived-chunk coverage
```

`deploy/server/install.sh` installs the user units on this host; see the comments there.

## Transport (ADR 0007)

Ingest binds `127.0.0.1:8480` (`ROOMLOG_BIND` in `~/.config/roomlog/service.env`). One-time,
after enabling HTTPS certificates in the Tailscale admin console:

```
tailscale serve --bg --https=443 http://127.0.0.1:8480
tailscale serve status
```

Every client, the local edge included, uses `https://oma.tailf63b9a.ts.net`. Nothing listens
on the tailnet interface, so no ufw rule. Fallback if `tailscale serve` is unavailable: bind
the tailnet address and open 8480 to named peers.
