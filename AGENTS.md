# roomlog

Two independent Python packages plus a shared contract. Docs, ADRs and plans live in
`~/hvelv/repos/roomlog/` (design: `spec-v1-design.md`), never here.

| Path | Owner | What |
|---|---|---|
| `contract/` | shared, pinned | sidecar schema, examples, wire protocol (`CONTRACT.md`) |
| `edge/` | edge track | `roomlog-edge` package: capture, timeline, VAD, chunker, spool, uploader, health |
| `server/` | server track | `roomlog-server` package: ingest, DB, worker, sessions, CLI, MCP, and `roomlog-reader` (`reader_http.py`, `recall.py`, `recall_routes.py`): the read-only recall HTTP API under `/v1/recall/*` that also serves `web/` |
| `web/` | web track | recall PWA: vanilla ES modules, no build step or dependencies, served same-origin by `roomlog-reader` (`static_root`). Gate: `cd web && node --test tests/*.test.mjs && node tests/browser.mjs` (the browser test runs headless `chromium` over the DevTools protocol against `contract/recall/` fixtures) |
| `deploy/edge/`, `deploy/server/` | same as above | install scripts, systemd units, example configs |
| `tests/e2e/` | shared | edge → ingest → worker → search, in-process. Gate: `cd server && uv run pytest -q ../tests/e2e` |
| `android/` | android track | Gradle project: `:core` (pure Kotlin mirrors of timeline, sidecar, spool, uploader, plus the Ogg Opus writer, raw segmenter, unsynced hold and upload loop) and `:app` (Compose; the P3 client: records, spools and uploads). Gate, with JDK 21 as `JAVA_HOME`: `./gradlew :core:test :app:assembleDebug`; see `android/README.md` |

Each package is a standalone uv project (`pyproject.toml`, `src/` layout, `tests/`,
`requires-python >= 3.11`; the server pins 3.12 in `.python-version`). Gate before commit,
run inside the package directory: `uv run pytest -q`. No model weights, no microphone and no
network are needed for the unit tests. Secrets never enter the repo; they live under
`~/.config/roomlog*/`. A change to `server/` runs the e2e gate too; a change to the recall
read API or `contract/recall/` runs the `web/` gate as well, since the browser test reads
those fixtures.

Tickets are GitHub issues on this repo (`gh issue ...`).
