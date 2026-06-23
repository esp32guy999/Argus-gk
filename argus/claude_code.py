"""Persistent Claude Code sessions — one long-lived `claude` stream-json subprocess
PER conversation, so each Forge thread keeps native CC context across turns (resumable
across restarts). Bypasses llama-swap entirely — no GPU model is touched.

Technique mined from Shane's peace app (the hard-won bits, written fresh here):
- drop the parent harness's CLAUDE_CODE_* / CLAUDECODE env so the nested cc doesn't
  think it's a child session;
- use ~/.claude directly (no isolated config dir — isolated copies go stale and
  cause token revocation races);
- 64MB stdout readline limit (stream-json emits one big JSON line per event);
- --session-id (new) / --resume (existing transcript) for durable per-thread sessions;
- workdir ~/argus + --setting-sources user,project so it picks up the homelab context
  (cc walks up from ~/argus and finds ~/CLAUDE.md, which @imports tools.md etc.).

NOTE: with cwd=~/argus and --dangerously-skip-permissions, this CC can READ AND WRITE
the whole ~/argus tree (its own code) and up-tree ~ — a Forge chat is a fully capable
agent on this box, not a sandbox.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

CLAUDE = os.environ.get("ARGUS_CLAUDE_BIN",
                        os.path.expanduser("~/.npm-global/bin/claude"))
CONFIG_DIR = os.environ.get("ARGUS_CC_CONFIG_DIR", os.path.expanduser("~/.claude"))
WORKDIR = os.environ.get("ARGUS_CC_WORKDIR", os.path.expanduser("~/argus"))  # homelab context via ~/CLAUDE.md up-tree
SESSIONS_FILE = Path(os.path.expanduser("~/argus/.cc_sessions.json"))
IDLE_TIMEOUT = float(os.environ.get("ARGUS_CC_IDLE", "1800"))   # evict sessions idle > 30 min
_WORKDIR_SLUG = WORKDIR.replace("/", "-")


def _sync_credentials() -> None:
    """No-op: we now use ~/.claude directly, so no sync needed."""
    pass


_IMG_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def _image_block(att: dict):
    """Turn a UI attachment {isImage, dataUrl} into an Anthropic image content
    block, or None if it isn't a usable base64 image data URL."""
    if not isinstance(att, dict) or not att.get("isImage"):
        return None
    url = att.get("dataUrl") or ""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64 = url.split(",", 1)
    media_type = header[5:].split(";", 1)[0]   # strip "data:" prefix
    if media_type not in _IMG_TYPES:
        return None
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _clean_env() -> dict:
    drop = ("CLAUDECODE", "CLAUDE_EFFORT", "AI_AGENT", "MEMORY_PRESSURE_WATCH")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE") and k not in drop}
    # Don't override CLAUDE_CONFIG_DIR — let claude use ~/.claude directly
    # so all processes share one token and refreshes don't revoke each other.
    env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def _load_sids() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}


def _save_sid(cid: str, sid: str) -> None:
    sids = _load_sids()
    sids[cid] = sid
    with contextlib.suppress(Exception):
        SESSIONS_FILE.write_text(json.dumps(sids))


def _transcript_exists(sid: str) -> bool:
    return (Path(os.path.expanduser("~/.claude")) / "projects" / _WORKDIR_SLUG / f"{sid}.jsonl").is_file()


def _parse_event(evt: dict) -> list[tuple]:
    """Map one stream-json event to internal queue items (pure, so it's testable).

    Returns a list of (kind, data) tuples:
      ("text", str)                                       — cumulative chat text
      ("event", {"kind":"thinking","text":...})           — reasoning block
      ("event", {"kind":"tool_use","name":...,"input":...})
      ("event", {"kind":"tool_result","text":...})        — truncated to 2000 chars
      ("done",  {"is_error":bool,"duration_ms":int|None})
    The `system/init` session-id capture stays in _read_stdout (it mutates state).
    """
    out: list[tuple] = []
    t = evt.get("type")
    if t == "assistant":
        for block in evt.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                out.append(("text", block["text"]))
            elif bt == "thinking" and block.get("thinking", "").strip():
                out.append(("event", {"kind": "thinking", "text": block["thinking"]}))
            elif bt == "tool_use":
                out.append(("event", {"kind": "tool_use",
                                      "name": block.get("name", "tool"),
                                      "input": block.get("input", {})}))
    elif t == "user":
        # tool_result blocks ride back on a synthetic user-role message
        for block in evt.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                out.append(("event", {"kind": "tool_result", "text": text[:2000]}))
    elif t == "result":
        out.append(("done", {"is_error": evt.get("is_error", False),
                             "duration_ms": evt.get("duration_ms")}))
    return out


class CCSession:
    """One persistent `claude` stream-json subprocess for one conversation."""

    def __init__(self, cid: str):
        self.cid = cid
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._q: asyncio.Queue | None = None     # current turn's event queue
        self._lock = asyncio.Lock()              # one turn at a time per session
        self.last_used = time.monotonic()

    async def _ensure(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        _sync_credentials()
        Path(WORKDIR).mkdir(parents=True, exist_ok=True)
        Path(CONFIG_DIR).mkdir(parents=True, exist_ok=True)
        stored = _load_sids().get(self.cid)
        if stored and _transcript_exists(stored):
            sid, sid_args = stored, ["--resume", stored]
        else:
            sid = str(uuid.uuid4())
            sid_args = ["--session-id", sid]
        args = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format",
                "stream-json", "--verbose", *sid_args, "--setting-sources", "user,project",
                "--dangerously-skip-permissions"]
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=WORKDIR, env=_clean_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=2 ** 26)   # 64MB: stream-json lines are big
        self.session_id = sid
        _save_sid(self.cid, sid)
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        p = self.proc
        while p and p.stderr:
            if not await p.stderr.readline():
                break

    async def _read_stdout(self) -> None:
        p = self.proc
        while p and p.stdout:
            line = await p.stdout.readline()
            if not line:
                if self._q:
                    await self._q.put(("error", "claude session ended"))
                break
            line = line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "system" and evt.get("subtype") == "init":
                self.session_id = evt.get("session_id", self.session_id)
                _save_sid(self.cid, self.session_id)
            for item in _parse_event(evt):
                if self._q:
                    await self._q.put(item)

    async def send(self, text: str, attachments=None):
        """Yield cumulative assistant text for this turn (loop.stream_run contract).
        Also yields ('__event__', {...}) activity markers so the server can drive the
        status pill AND the tap-to-expand activity stream (claude-code path only):
          {"kind":"thinking","text":...}, {"kind":"tool_use","name":...,"input":...},
          {"kind":"tool_result","text":...}, {"kind":"done","duration_ms":...}.

        `attachments` (optional) is the UI's list of staged files
        [{filename, isImage, dataUrl}]; image data URLs become Anthropic image
        content blocks so the nested cc sees the photo (vision)."""
        async with self._lock:
            await self._ensure()
            self.last_used = time.monotonic()
            self._q = asyncio.Queue()
            content = [b for a in (attachments or []) for b in (_image_block(a),) if b]
            if text or not content:
                content.append({"type": "text", "text": text})
            payload = {"type": "user",
                       "message": {"role": "user", "content": content}}
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self.proc.stdin.drain()
            acc = ""
            while True:
                kind, data = await self._q.get()
                if kind == "text":
                    acc += ("\n\n" if acc else "") + data
                    yield acc
                elif kind == "event":
                    yield ("__event__", data)
                elif kind == "done":
                    yield ("__event__", {"kind": "done",
                                         "duration_ms": data.get("duration_ms")})
                    break
                elif kind == "error":
                    if not acc:
                        raise RuntimeError(data)
                    break
            self.last_used = time.monotonic()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)


_SESSIONS: dict[str, CCSession] = {}


async def _evict_idle() -> None:
    now = time.monotonic()
    for cid, s in [(c, x) for c, x in _SESSIONS.items() if now - x.last_used > IDLE_TIMEOUT]:
        await s.close()
        _SESSIONS.pop(cid, None)


async def send(conversation_id: str, text: str, attachments=None):
    """Module entry point: stream a turn through the conversation's persistent session."""
    await _evict_idle()
    s = _SESSIONS.get(conversation_id)
    if s is None:
        s = _SESSIONS[conversation_id] = CCSession(conversation_id)
    async for chunk in s.send(text, attachments=attachments):
        yield chunk
