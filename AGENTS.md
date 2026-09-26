# roomlog

Two independent Python packages plus a shared contract. Docs, ADRs and plans live in
`~/hvelv/repos/roomlog/` (design: `spec-v1-design.md`), never here.

| Path | Owner | What |
|---|---|---|
| `contract/` | shared, pinned | sidecar schema, examples, wire protocol (`CONTRACT.md`) |
| `edge/` | edge track | `roomlog-edge` package: capture, timeline, VAD, chunker, spool, uploader, health |
| `server/` | server track | `roomlog-server` package: ingest, DB, worker, sessions, CLI, MCP |
| `deploy/edge/`, `deploy/server/` | same as above | install scripts, systemd units, example configs |
| `tests/e2e/` | shared | edge → ingest → worker → search, in-process |

Each package is a standalone uv project (`pyproject.toml`, `src/` layout, `tests/`,
`requires-python >= 3.11`; the server pins 3.12 in `.python-version`). Gate before commit,
run inside the package directory: `uv run pytest -q`. No model weights, no microphone and no
network are needed for the unit tests. Secrets never enter the repo; they live under
`~/.config/roomlog*/`.

Tickets are GitHub issues on this repo (`gh issue ...`).
