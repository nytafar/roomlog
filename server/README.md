# roomlog-server

Ingest, archive, transcription worker, sessions, CLI (`roomlog`) and MCP server (`roomlog-mcp`).
Design: `~/hvelv/repos/roomlog/spec-v1-design.md`. Gate: `uv run pytest -q` in this directory
(no model weights, no network).

## Layout

| Module | What |
|---|---|
| `config.py` | `server.toml` + `tokens.toml`; every path comes from here |
| `db.py` | SQLite schema, WAL, `PRAGMA user_version` migrations, FTS5 (unicode61 + trigram) |
| `ingest.py`, `archive.py`, `sidecar.py` | `PUT /v1/chunks/{sha}` per `contract/CONTRACT.md`; temp+fsync+rename |
| `backends/` | `local` (faster-whisper, lazy), `openai` (Berget and friends), `whisper_cpp`, `fake`; `Router` = first healthy, failover on connection error / timeout / 5xx |
| `worker.py` | claim → decode → window (≤ 30 s, 0.3 s gaps) → transcribe → map words to chunks → filter → write → sessionize |
| `sessions.py` | gap-and-island per device, deterministic ids, full rebuild in one transaction |
| `queries.py` | read-only queries shared by the CLI and MCP tools |
| `cli.py`, `mcp_server.py` | `roomlog …`, `roomlog-mcp` (stdio) |

## Run

```
uv sync
uv run roomlog --help
uv run roomlog -c ~/.config/roomlog/server.toml status
```

`deploy/server/install.sh` installs the user units on this host; see the comments there.
