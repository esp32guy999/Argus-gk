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
import json
import threading
from typing import Any

import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

KEEPALIVE: list["MCPConnection"] = []   # connections live for the process lifetime

# ── Write verification ────────────────────────────────────────────────────────
# MCP tools are passed straight through from an external server, so a state-changing
# call that quietly does nothing (or that HA answers with a "Sorry…" *speech* string
# instead of a protocol error) reaches the model as hollow success — it can't close
# the loop on feedback it never got. For WRITE tools we inspect the result and:
#   - escalate a no-op / failure into a ModelRetry the model must act on, and
#   - enrich a real success with WHAT changed (ground truth), so the model (and the
#     transcript) can verify the effect instead of trusting a canned phrase.
# Read tools (Get*/List*/context) pass through untouched. Per-server opt-out via
# `verify_writes: false`; explicit `read_tools`/`write_tools` lists override the guess.
_WRITE_HINTS = (
    "turnon", "turnoff", "toggle", "set", "add", "remove", "delete", "complete",
    "cancel", "broadcast", "pause", "unpause", "play", "mute", "unmute", "next",
    "previous", "lock", "unlock", "open", "close", "press", "start", "stop",
    "increase", "decrease", "boost", "activate", "trigger", "send", "create",
)
_FAIL_PHRASES = (
    "sorry", "not aware", "no valid target", "couldn't find", "could not find",
    "can't find", "cannot find", "unable to", "no matching", "don't have any",
    "do not have", "not found", "no entities", "failed to",
)


def _classifies_write(name: str, server: dict) -> bool:
    """Best-effort: is this MCP tool a state-changing (write) call?"""
    if name in server.get("read_tools", []):
        return False
    if name in server.get("write_tools", []):
        return True
    n = name.lower()
    if n.startswith("get") or n.startswith("list") or n.endswith("_get_items") \
            or "livecontext" in n or "datetime" in n or "status" in n:
        return False
    return any(v in n for v in _WRITE_HINTS)


def _find_key(obj, key):
    """Recursively pull the first value for `key` out of a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _verify_write(name: str, text: str, structured):
    """Return (ok, message, enriched_text) for a write result.

    ok=False -> caller raises ModelRetry(message). ok=True -> return enriched_text.
    Uses HA's structured success/failed/code when present — HA's MCP server returns
    that block as a JSON *text* string rather than structuredContent, so we parse the
    text too — and falls back to failure-phrase detection on the speech string. When
    neither yields a signal the call passes through unchanged (never block what we
    can't verify)."""
    data = structured
    if data is None and text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                data = parsed
        except (ValueError, TypeError):
            pass
    success = _find_key(data, "success")
    failed = _find_key(data, "failed")
    code = _find_key(data, "code")                  # e.g. no_valid_targets
    low = (text or "").lower()
    phrase_fail = any(p in low for p in _FAIL_PHRASES)

    if code or phrase_fail or (isinstance(success, list) and not success and not failed):
        detail = text or (f"error code {code!r}" if code else "no entities matched")
        return (False,
                f"{name} changed nothing — {detail}. Verify the target exists and is the "
                f"right type (e.g. porch lights are `switch.*`, not `light.*`), then retry "
                f"or report honestly that it didn't work — do NOT claim success.",
                text)
    if isinstance(failed, list) and failed:
        return (False, f"{name}: some targets failed: {failed}. {text or ''}".strip(), text)
    if isinstance(success, list) and success:
        changed = ", ".join(s.get("name") or s.get("id", "?")
                            for s in success if isinstance(s, dict)) or str(success)
        return (True, "", f"{text}\n[verified changed: {changed}]".strip())
    return (True, "", text)


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
        structured = getattr(result, "structuredContent", None)
        if getattr(result, "isError", False):
            raise ModelRetry(f"mcp tool '{name}' error: {text or 'unknown error'}")
        # Verify state-changing calls so a no-op/failure can't pass as hollow success.
        if self.spec.get("verify_writes", True) and _classifies_write(name, self.spec):
            ok, msg, enriched = _verify_write(name, text, structured)
            if not ok:
                raise ModelRetry(msg)
            return enriched or structured or ""
        return text or structured or ""

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
