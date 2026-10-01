# roomlog-server

Ingest, archive, transcription worker, sessions, CLI (`roomlog`) and MCP server (`roomlog-mcp`).
Design overview in the top-level README; raw segments and server-side segmentation are
decision 5 there. Gate: `uv run pytest -q` in this directory (no model weights, no network;
ffmpeg encodes the test audio).

## Layout

| Module | What |
|---|---|
| `config.py` | `server.toml` + `tokens.toml`; every path comes from here |
| `db.py` | SQLite schema, WAL, `PRAGMA user_version` migrations (v3: `raw_segments`, `raw_progress`, `chunks.kind`; v4: `dictation_spans`, `tags`, `segments.device_id/span_id/superseded_by`, `lang NOT NULL`), FTS5 (unicode61 + trigram) |
| `ingest.py`, `archive.py`, `sidecar.py` | `PUT /v1/chunks/{sha}`, `GET /v1/time`, `POST /v1/spans` per `contract/CONTRACT.md`; temp+fsync+rename; `kind: raw` lands under `archive/raw/` |
| `spans.py` | dictation spans and tags (ADR 0008): validation, storage, the deterministic tags a span carries |
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

A gap inside an epoch (a network outage longer than `raw_idle_s` while the phone keeps
recording) closes the chain before it as final. When the continuation arrives, speech that
began less than 250 ms before that seam can be dropped as too short on the far side: at
most about 250 ms per such seam, by design (the chunker's `min_speech`).

`[segmenter] vad = "energy"` is an RMS gate for tests and for bootstrapping before
`fetch-model`; it is not for production. The 30-day `purge-raw` deletes raw audio that
overlaps no derived chunk, so with the energy gate it would delete on the gate's decisions.
Existing `server.toml` files without a `[segmenter]` section get the defaults (`silero`,
`raw_idle_s = 120`, thresholds 0.5 / 0.35); see `deploy/server/server.toml.example`.

## Dictation spans and tags (ADR 0008)

The room mic is also the dictation mic. The dictation tool posts one span per dictation
(`POST /v1/spans`: start, end, text, lang, engine, mode, target app, cancelled). The worker:

- applies pending spans first: the text becomes one `segments` row with `lang` from the span,
  `model_id` = the engine, `span_id` set, tagged `channel=dictation`, `mode=…`, `app=…`;
  STT rows that overlap the span are marked `superseded_by` (never deleted);
- claims a chunk only once its end is `[worker] dictation_hold_s` (20 s) old, so the span
  usually arrives first; audio inside a span is zeroed before STT, and a chunk with under
  1 s left outside spans is marked done with `model_id = "dictation"` and no STT call;
- leaves the audio of a cancelled or empty span to STT but tags its rows
  `channel=dictation`, `cancelled=true`, so it is never read as an open command.

Tags are `{target, key, value, source, origin}`: `target` is a transcript row (`segment_id`)
or a device time span (`device_id`, `start_utc_ms`, `end_utc_ms`); `source` is
`deterministic` or `model`; `origin` names the tool. A row's `channel` is its latest
`channel` tag, `ambient` when it has none. Search, session transcripts, context and export
hide superseded rows always and dictation rows unless asked (`--include-dictation`,
`include_dictation=true`); every row reports `lang` and `channel`. `roomlog spans` and the
`list_spans` tool show the spans as received.

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
