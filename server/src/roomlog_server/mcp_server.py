"""`roomlog-mcp`: stdio MCP server with typed tools over the read-only DB (§4.6).

Built on the official `mcp` package. In `mcp` 2.x `FastMCP` is called `MCPServer`; the
decorator-based tool API is the same. The tool functions are plain and testable without
a transport; `build_server` registers them.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import db as dbmod
from .config import Config, load_config
from .times import parse_user_time


class Tools:
    """The tool implementations, one read-only connection per call."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _conn(self):
        assert self.cfg.db_path is not None
        if not self.cfg.db_path.exists():
            raise RuntimeError(f"no database at {self.cfg.db_path}; nothing has been ingested yet")
        return dbmod.connect(self.cfg.db_path, readonly=True)

    def search(self, query: str, from_utc: str | None = None, to_utc: str | None = None,
               device_id: str | None = None, limit: int = 10, offset: int = 0,
               fuzzy: bool = False, include_dictation: bool = False) -> list[dict[str, Any]]:
        from .queries import search
        conn = self._conn()
        try:
            return search(conn, query, parse_user_time(from_utc), parse_user_time(to_utc),
                          device_id, limit, offset, fuzzy, include_dictation)
        finally:
            conn.close()

    def list_spans(self, from_utc: str | None = None, to_utc: str | None = None,
                   device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from .queries import list_spans
        conn = self._conn()
        try:
            return list_spans(conn, parse_user_time(from_utc), parse_user_time(to_utc), device_id, limit)
        finally:
            conn.close()

    def list_sessions(self, from_utc: str | None = None, to_utc: str | None = None,
                      device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        from .queries import list_sessions
        conn = self._conn()
        try:
            return list_sessions(conn, parse_user_time(from_utc), parse_user_time(to_utc), device_id, limit)
        finally:
            conn.close()

    def list_devices(self) -> list[dict[str, Any]]:
        from .queries import list_devices
        conn = self._conn()
        try:
            return list_devices(conn)
        finally:
            conn.close()

    def get_session(self, session_id: str, max_chars: int = 20000,
                    include_dictation: bool = False) -> dict[str, Any]:
        from .queries import get_session, middle_truncate, transcript_lines
        conn = self._conn()
        try:
            s = get_session(conn, session_id, include_dictation)
        finally:
            conn.close()
        if s is None:
            raise ValueError(f"no session {session_id!r}")
        text = "\n".join(transcript_lines(s.pop("segments")))
        s["transcript"] = middle_truncate(text, max_chars)
        s["truncated"] = len(text) > max_chars
        return s

    def get_segment_context(self, segment_id: int, window_s: float = 60.0,
                            include_dictation: bool = False) -> dict[str, Any]:
        from .queries import get_segment_context
        conn = self._conn()
        try:
            ctx = get_segment_context(conn, segment_id, window_s, include_dictation)
        finally:
            conn.close()
        if ctx is None:
            raise ValueError(f"no segment {segment_id}")
        return ctx


def build_server(cfg: Config):
    from mcp.server.mcpserver import MCPServer

    tools = Tools(cfg)
    server = MCPServer(
        name="roomlog",
        instructions=(
            "Search and read transcripts of room audio captured by roomlog. Times are UTC ISO 8601. "
            "Use search for keywords (fuzzy=true for substrings/misspellings), list_sessions to browse, "
            "get_session for a whole transcript and get_segment_context for what was said around a hit. "
            "Every row carries lang and channel. Rows on the dictation channel (text the owner dictated "
            "into an app, already acted on) are left out unless include_dictation=true; list_spans shows "
            "the dictations as received."
        ),
    )

    @server.tool(description="Full-text search over transcript segments (bm25; fuzzy=true uses trigram substring matching). Optional UTC time range and device_id filter. Dictation rows only with include_dictation=true.")
    def search(query: str, from_utc: str | None = None, to_utc: str | None = None,
               device_id: str | None = None, limit: int = 10, offset: int = 0,
               fuzzy: bool = False, include_dictation: bool = False) -> list[dict[str, Any]]:
        return tools.search(query, from_utc, to_utc, device_id, limit, offset, fuzzy, include_dictation)

    @server.tool(description="List dictation spans (what the owner dictated into which app, when, in which language), newest first, with optional UTC range and device_id filter.")
    def list_spans(from_utc: str | None = None, to_utc: str | None = None,
                   device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return tools.list_spans(from_utc, to_utc, device_id, limit)

    @server.tool(description="List sessions (conversations separated by silence), newest first, with optional UTC range and device_id filter.")
    def list_sessions(from_utc: str | None = None, to_utc: str | None = None,
                      device_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return tools.list_sessions(from_utc, to_utc, device_id, limit)

    @server.tool(description="List capture devices with chunk counts and last-seen times.")
    def list_devices() -> list[dict[str, Any]]:
        return tools.list_devices()

    @server.tool(description="One session with its transcript as [HH:MM:SS] lines, middle-truncated to max_chars. Dictation rows only with include_dictation=true.")
    def get_session(session_id: str, max_chars: int = 20000, include_dictation: bool = False) -> dict[str, Any]:
        return tools.get_session(session_id, max_chars, include_dictation)

    @server.tool(description="The segments spoken within window_s seconds around one segment (same device). Dictation rows only with include_dictation=true.")
    def get_segment_context(segment_id: int, window_s: float = 60.0, include_dictation: bool = False) -> dict[str, Any]:
        return tools.get_segment_context(segment_id, window_s, include_dictation)

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="roomlog-mcp", description="roomlog MCP server (stdio), read-only.")
    p.add_argument("-c", "--config", help="server.toml (default ~/.config/roomlog/server.toml or $ROOMLOG_CONFIG)")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    build_server(cfg).run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
