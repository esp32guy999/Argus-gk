"""MCP tool lane — consume Model Context Protocol servers as Argus tools.

Integration glue: MCP is async, but the registry dispatches synchronously, so each
server runs on its own persistent background event loop and we bridge sync->async
via run_coroutine_threadsafe. The client contexts are entered AND exited inside one
long-lived task (`_manage`) to avoid anyio's "cancel scope in a different task" error.

Transports: 'stdio' (local subprocess), 'http' (Streamable HTTP), and 'sse'
(legacy SSE, used by e.g. Home Assistant's MCP server).
Exposes the same synchronous registry.Tool contract as every other lane.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

KEEPALIVE: list["MCPConnection"] = []   # connections live for the process lifetime


class MCPConnection:
    """A live connection to one MCP server, driven on a background event loop."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.timeout = spec.get("timeout", 30)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._fut = None

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._fut = asyncio.run_coroutine_threadsafe(self._manage(), self._loop)
        if not self._ready.wait(self.timeout):
            if self._fut.done():
                self._fut.result()  # surface the real connection error
            raise TimeoutError(f"MCP server '{self.name}' did not become ready")

    async def _manage(self) -> None:
        """Enter transport + session, signal ready, hold open until stopped —
        all in one task so the contexts also EXIT in this task."""
        from mcp import ClientSession
        transport = self.spec.get("transport", "stdio")
        if transport == "http":
            from mcp.client.streamable_http import streamablehttp_client
            cm = streamablehttp_client(self.spec["url"], headers=self.spec.get("headers"))
        elif transport == "sse":
            from mcp.client.sse import sse_client
            cm = sse_client(self.spec["url"], headers=self.spec.get("headers"))
        else:
            from mcp.client.stdio import stdio_client, StdioServerParameters
            cm = stdio_client(StdioServerParameters(
                command=self.spec["command"], args=self.spec.get("args", [])))
        async with cm as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._stop = asyncio.Event()
                self._ready.set()
                await self._stop.wait()

    def _call_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.timeout)

    def list_tools(self) -> list:
        return self._call_async(self._session.list_tools()).tools

    def call(self, name: str, arguments: dict) -> Any:
        try:
            result = self._call_async(self._session.call_tool(name, arguments))
        except ModelRetry:
            raise
        except Exception as err:  # transport/timeout -> teaching message
            raise ModelRetry(f"mcp tool '{name}' on '{self.name}' failed: {err}")
        text = "\n".join(
            getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", "")
        )
        if getattr(result, "isError", False):
            raise ModelRetry(f"mcp tool '{name}' error: {text or 'unknown error'}")
        return text or getattr(result, "structuredContent", None) or ""

    def close(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._fut:
            try:
                self._fut.result(timeout=10)
            except Exception:
                pass
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


def _wrap(conn: MCPConnection, mcp_tool, tags: list) -> Tool:
    name = mcp_tool.name  # captured per-tool (no late-binding bug)

    def dispatch(**kwargs):
        return conn.call(name, kwargs)

    return Tool(
        name=name,
        description=mcp_tool.description or name,
        tags=list(tags),
        func=dispatch,
        provider="mcp",
        schema=mcp_tool.inputSchema,   # external schema -> registry uses Tool.from_schema
    )


def tools(manifest_path: str = "config/mcp_servers.yaml", *, _sink: list | None = None) -> list[Tool]:
    """Provider entry point: connect to each MCP server, emit its tools.
    Connections are kept alive (KEEPALIVE, or _sink for deterministic test cleanup)."""
    with open(manifest_path) as f:
        config = yaml.safe_load(f)
    keep = _sink if _sink is not None else KEEPALIVE
    out: list[Tool] = []
    for server in config.get("servers", []):
        conn = MCPConnection(server)
        conn.start()
        keep.append(conn)
        tags = server.get("tags", [])
        for mt in conn.list_tools():
            out.append(_wrap(conn, mt, tags))
    return out
