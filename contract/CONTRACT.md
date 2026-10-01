# roomlog edge ↔ server contract, version 1

Pinned 2026-09-26. Both `edge/` and `server/` build against this file, `sidecar.schema.json`
and `examples/`. Additive changes to the sidecar never bump `schema_version`; servers store
unknown fields and ignore them. The design overview is in the top-level README.

## Chunk

One Ogg Opus file (16 kHz mono, 24 kbps, speech mode) of at most 30.0 s: speech plus up to
300 ms of padding on each side. Identity = SHA-256 of the `.opus` bytes. The sidecar is not
part of the hash and may be revised before upload (unsynced-clock re-stamping).

Ogg comment tags carry only immutable facts: `ROOMLOG_DEVICE_ID`, `ROOMLOG_RUN_ID`,
`ROOMLOG_EPOCH`, `ROOMLOG_N_START`, `ROOMLOG_EDGE_VERSION`. `start_utc` is sidecar-only.

## Sidecar

See `sidecar.schema.json` (JSON Schema 2020-12) and `examples/`. Required fields:
`schema_version`, `device_id`, `sha256`, `start_utc`, `duration_s`, `run_id`, `epoch`,
`discontinuity`, `clock_synced`.

`kind` (added 2026-09-26, additive): `"speech"` when absent, or `"raw"`. A raw segment is
continuous audio cut on the sample counter every 480 000 samples (30.0 s): no padding, no
`vad` object, `n_start` and `n_samples` required, `n_samples == 480000` except for the last
segment of an epoch or run, which is shorter. Full segments carry `cut_reason: "cap"`; the
short last one carries `"discontinuity"` or `"shutdown"`. `duration_s` equals
`n_samples / 16000` within 1 ms. Within one `(run_id, epoch)`,
`n_start + n_samples` of a segment equals `n_start` of the next. The server runs VAD and
the chunker over raw segments itself (ADR 0005); speech chunks are what the Linux edge sends.

Timing semantics:

- `run_id` identifies one capture-process lifetime; `epoch` counts sample-continuity
  segments inside it. Within one `(run_id, epoch)` the sample timeline has no gaps, so
  `start_utc` of consecutive chunks is exact relative to each other.
- `discontinuity: true`: an unknown amount of audio was lost immediately before this chunk.
  A chunk never spans a discontinuity.
- `clock_step: true`: the system clock stepped while the chunk was open; `start_utc` comes
  from the corrected (post-step) mapping.
- `clock_synced: false`: the stamping clock was not verified against a trusted reference
  (the kernel NTP flag on Linux edges, a successful `GET /v1/time` probe within the last ten
  minutes on Android; ADR 0006); `start_utc` may be off by the boot-clock error.
- `cut_reason`: `silence` | `cap` | `discontinuity` | `shutdown`.
- `session_hint`, `multi_speaker`: reserved for v2, always `null` in v1.

## Upload

```
PUT /v1/chunks/{sha256}
Authorization: Bearer <device token>
Content-Type: audio/ogg
Content-Length: <n>
X-Roomlog-Meta: <sidecar JSON on one line, ASCII only (json.dumps(..., ensure_ascii=True, separators=(",", ":")))>
<body: the .opus bytes>
```

| Status | Meaning | Edge action |
|---|---|---|
| `201` | stored now | ack: delete local `.opus` and `.json` |
| `200` | already stored with this sha | ack: delete local files |
| `401` | missing or unknown token | keep; retry with backoff; health fails |
| `403` | token's device ≠ `device_id` | keep; retry with backoff; health fails |
| `409` | body sha ≠ URL sha, or ≠ `meta.sha256` | move to `spool/failed/`; never retry |
| `413` | body larger than 8 MiB | move to `spool/failed/` |
| `422` | sidecar fails the schema, or header missing/unparseable | move to `spool/failed/` |
| `411` | missing `Content-Length` (chunked bodies not accepted) | keep; retry with backoff (client bug) |
| `5xx`, timeout, connection error | server problem | keep; retry with backoff |

Success body (both 200 and 201):

```json
{"sha256": "<hex64>", "status": "created" | "exists", "path": "2026/09/26/20260926T101532417Z_9f2c1a3b.opus"}
```

The edge deletes local files only when the response `sha256` equals its own. Error body:
`{"error": "<message>"}`.

Backoff on retryable failures: `min(300, 2**k)` seconds with full jitter, reset on success.
Upload order: oldest `start_utc` first (lexical order of the spool filenames).

## Other endpoints

- `GET /healthz` → `200 {"ok": true}`; no auth; for monitors.
- `GET /v1/whoami` → `200 {"device_id": "<id>"}` with a valid bearer; `401` otherwise. Used by
  the edge selftest.
- `GET /v1/time` → `200 {"utc_ns": <server UTC, integer nanoseconds>}` with a valid bearer;
  `401` otherwise. Clients without an NTP flag measure their offset to the server with one
  round trip and stamp in the server's timebase (ADR 0006).

## Dictation spans

A device whose microphone also serves a dictation tool reports each dictation as a span
(ADR 0008). The server stores the span's text as the transcript of that time instead of
running STT on the audio; nothing is transcribed twice.

```
POST /v1/spans
Authorization: Bearer <device token>
Content-Type: application/json
Content-Length: <n>                      (at most 65 536 bytes)

{"start_utc": "2026-10-01T12:00:00.000Z", "end_utc": "2026-10-01T12:00:06.250Z",
 "text": "restart the worker", "lang": "en", "engine": "voxtype/parakeet-tdt-0.6b-v3-int8",
 "mode": "raw", "target": {"app": "ghostty", "window": "~/code/roomlog"},
 "cancelled": false, "origin": "dictation-span/1"}
```

- `start_utc`, `end_utc`: the sidecar timestamp form; `end_utc >= start_utc`, at most one hour apart.
- `text`: what the engine produced, with the client's own fixed vocabulary corrections if it
  has any, before any LLM rewrite or edit; empty when nothing was heard.
- `lang`: the language the engine was run with, which becomes the row's `lang`.
- `engine`: free text naming the STT engine and model.
- `mode`: `raw`, `cleanup` (the text was then rewritten by an LLM before it was typed) or
  `edit-instruction` (the text is a spoken instruction applied to a selection).
- `target`: optional; `app` is tagged on the row, `window` is kept on the span only.
- `cancelled`: the dictation was discarded; an empty `text` counts as cancelled too. The audio
  of a cancelled span is transcribed normally but its rows stay on the dictation channel. A
  client that never saw the dictation end should bound `end_utc` (a minute from the start is
  the dictation tool's own limit); the server clamps a cancelled span to ten minutes.
- `origin`: the tool and version that produced the span; it becomes every tag's `origin`.
- `device_id`: optional; when present it must equal the token's device.

| Status | Meaning | Client action |
|---|---|---|
| `201` | stored | done |
| `200` | a span with this device and `start_utc` is already stored | done (a retry after a lost reply) |
| `401` | missing or unknown token | keep; retry with backoff |
| `403` | `device_id` ≠ the token's device | drop |
| `411` | missing `Content-Length` | client bug |
| `413` | body larger than 64 KiB | drop |
| `422` | body invalid (`{"error": "span invalid: ..."}`) | drop |
| `5xx`, timeout, connection error | server problem | keep; retry with backoff |

Success body: `{"id": <span id>, "status": "created" | "exists", "device_id": "<id>"}`.

The client must never let this call hold up the dictation itself: post from a detached
process (or after the text has been delivered), with a short timeout, and spool failures for
a retry with backoff that does not wait for the next dictation. A span posted late still
wins: STT rows written for its time are marked superseded, not deleted. The time stamps
should bracket the recording (stamp the start before the engine starts and the end after it
stops, or pad it); STT output that still crosses a span edge is cut at the edge when the
backend gives word times, the part inside being superseded by the span.

## Auth and transport

Per-device bearer token, configured on the server as `device_id = "token"`, compared in
constant time. The server rejects (`403`) a sidecar whose `device_id` differs from the
token's device. Transport is HTTPS on the tailnet name published by `tailscale serve`
(ADR 0007; plain HTTP on the tailnet address was the original v1 setup); the edge takes the
full base URL from config and assumes nothing about the scheme.

## Archive layout (server)

`archive/YYYY/MM/DD/<start_utc compact>_<sha8>.opus` and `.json`, partitioned by
`start_utc` in UTC (`20260926T101532417Z` form), sidecar stored byte-for-byte as received.
Raw segments live under `archive/raw/YYYY/MM/DD/` with the same naming. The server purges
raw segments that overlap no speech chunk after 30 days (fixed in v1, configurable in v2).
