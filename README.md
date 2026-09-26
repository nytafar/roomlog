# roomlog

Always-on capture of one person thinking out loud in a room, turned into timestamped, searchable text for that person and their AI agents.

## Why

Most of the thinking that matters happens out loud, alone, and evaporates. Meeting recorders and wearables exist for this and fail at it in the same ways: they stream to someone else's cloud, they die with their battery, they lose audio whenever the connection drops, and they stamp segments with whatever the wall clock said, which drifts, jumps and hides gaps. None of them are built to run for months on a shelf.

roomlog is built for that shelf. A cheap device in the room listens all the time, a machine you own turns speech into text, and everything is indexed so that "what was I saying about the ingest retry policy last Tuesday" is a query, for you at a terminal or for an agent through MCP.

## What it solves

- **Capture without ceremony.** Nothing to start, nothing to charge, nothing to remember. The device records whenever there is speech and stays quiet otherwise.
- **Timestamps you can trust.** Times come from a sample counter anchored to UTC, not from the clock at the moment a file opened. Inside one continuous stretch, two chunks are exact relative to each other to the sample; every gap, clock step and unsynced clock is marked in the data rather than papered over.
- **Nothing lost to the network.** Audio is written to local storage first and uploaded with an idempotent, content-addressed PUT. Outages, reboots and retries cannot duplicate or drop a chunk.
- **Your hardware, your server.** Transport is the tailnet; storage is a directory of Ogg Opus files and one SQLite database on a workstation. Transcription can use a cloud model, but that is a config choice with a local fallback, not a dependency.
- **Search, sessions, agents.** Full-text search with Norwegian diacritics intact, fuzzy search, sessions derived from silence gaps, a CLI, a Markdown export for a notes vault, and an MCP server so agents can search and read transcripts without touching SQL.
- **More than one room.** Every device has its own id and token; sessions, search and status are per device.

## How it works

```
 room ─▶ client ─▶ Ogg Opus chunk + JSON sidecar ─▶ spool ─▶ PUT /v1/chunks/{sha256}
                                                                      │
                 ┌────────────────────────────────────────────────────┘
                 ▼
             ingest ─▶ archive/ (files) + SQLite chunks row
                                          │
                                       worker ─▶ STT backend chain ─▶ segments (+ FTS) ─▶ sessions
                                                                                            │
                                                                     roomlog CLI · roomlog-mcp ◀┘
```

Everything is small parts joined by files and one HTTP verb. Each part is a pure module where it can be, tested without a microphone, model weights or network.

### Two kinds of client

| Client | Sends | VAD and chunking |
|---|---|---|
| Linux edge (Raspberry Pi, any box with a mic) | speech chunks, at most 30 s, cut by Silero VAD | on the device |
| Android thin client (phone or dedicated appliance) | fixed 30 s raw segments of continuous audio | on the server |

Both speak the same contract. The Android client is deliberately thin: capture, encode with the platform codec, spool, upload. The server runs the same VAD and chunker over raw segments that the Pi runs locally, which also means segmentation can be re-run over history when the VAD improves.

### The contract

A chunk is one Ogg Opus file (16 kHz mono, 24 kbps) and a JSON sidecar. The file's SHA-256 is its identity and its URL. Ogg tags carry only immutable facts (device, run, epoch, first sample); everything that might be corrected later, above all the UTC start time, lives in the sidecar. The server stores the sidecar byte for byte and ignores fields it does not know, so the contract grows without version bumps. Full text in [`contract/CONTRACT.md`](contract/CONTRACT.md).

### Time

A client keeps a 64-bit sample counter and one anchor per *epoch*, a stretch with no lost samples. `start_utc = anchor_utc + (n_start − anchor_n) / 16000`. A new epoch begins only when samples were lost; a chunk never spans one. A system clock step moves the anchor's UTC and nothing else, so the open chunk is re-stamped rather than torn. Microphone crystal drift is absorbed by re-anchoring once a minute. Clients without a trustworthy NTP flag measure their offset to the server instead and stamp in the server's timebase. Chunks recorded before any sync are held and re-stamped when sync arrives.

### Transcription

The worker packs pending chunks into windows of up to 30 s, sends each window to the first healthy backend in an ordered chain, and maps words back to chunks by their midpoints so a segment that crosses a chunk boundary is split there. Backends are one interface with three adapters: any OpenAI-compatible endpoint (Berget AI running NB-Whisper large, with word alignment), local faster-whisper (NB-Whisper medium, int8), and a whisper.cpp server. Every segment records which model produced it. Hallucination filters and a boilerplate blocklist run before anything is indexed.

### Sessions and search

Sessions are islands of chunks per device with no gap longer than five minutes, computed in SQL and rebuilt after every batch; their ids are deterministic so a rebuild never renumbers anything. Search is FTS5 bm25 with diacritics preserved, plus a trigram index for fuzzy matching. The CLI and the MCP server open the database read-only.

## Components

| Path | What |
|---|---|
| [`contract/`](contract/) | Sidecar schema, examples, wire protocol. Pinned; both sides build against it. |
| [`edge/`](edge/) | `roomlog-edge`: capture, timeline, Silero VAD, chunker, Opus encode, spool, uploader, health. Python. |
| [`server/`](server/) | `roomlog-server`: ingest, archive, SQLite, worker, backends, sessions, CLI, MCP. Python. |
| [`android/`](android/) | Kotlin thin client: `:core` (timeline, Ogg Opus writer, sidecar, spool, uploader, clock offset) and the app. |
| [`deploy/`](deploy/) | Install scripts, systemd units and example configs for edge and server. |
| [`tests/e2e/`](tests/e2e/) | Capture → ingest → worker → search, in-process, with a fake transcriber. |

## Running it

Server, on the workstation that will hold the archive:

```
deploy/server/install.sh        # uv venv, config from examples, user systemd units
roomlog fetch-model             # NB-Whisper medium (about 3 GB) and Silero VAD
roomlog selftest                # DB, archive, tokens, every configured backend
systemctl --user enable --now roomlog-ingest roomlog-worker roomlog-health.timer
tailscale serve --bg --https=443 http://127.0.0.1:8480
```

Linux edge, on the device in the room:

```
deploy/edge/install.sh --mode system     # or --mode user on a desktop
roomlog-edge devices                     # pick the input
roomlog-edge selftest                    # model, VAD, encoder, clock, server auth
systemctl enable --now roomlog-edge-capture roomlog-edge-uploader roomlog-edge-health.timer
```

Android: see [`android/README.md`](android/README.md).

Then talk, and:

```
roomlog status
roomlog search "ingest retry"
roomlog sessions --from 2026-09-26
roomlog session <id>
roomlog export --vault ~/notes/roomlog
```

Each package is a standalone `uv` project; `uv run pytest -q` inside `edge/` or `server/` is the gate and needs no hardware.

## Status

As of 2026-09-26: the server and the Linux edge are implemented, reviewed and deployed on the workstation; the contract has been extended for raw segments, a server time endpoint and HTTPS over `tailscale serve`; the server-side segmenter and the Android client are in progress. Nothing has yet been recorded from a real room.

## Design notes

Design, ADRs, research and the running handoff log live outside the repo, in the owner's notes vault under `repos/roomlog/`. The decisions that shaped the code, in short:

1. Content-addressed `PUT`, not a stream or a resumable protocol.
2. Sample-counter timeline with epochs, not wall-clock stamps.
3. Sessions derived per device by silence gap, with deterministic ids.
4. Transcription through an ordered backend chain, cloud first, local fallback.
5. Thin clients upload raw 30 s segments; the server runs VAD and the chunker.
6. Clients may use the server's clock as their sync reference.
7. Ingest on loopback, published over HTTPS by `tailscale serve`.
