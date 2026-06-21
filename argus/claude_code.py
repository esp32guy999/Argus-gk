"""Persistent Claude Code sessions — one long-lived `claude` stream-json subprocess
PER conversation, so each Forge thread keeps native CC context across turns (resumable
across restarts). Bypasses llama-swap entirely — no GPU model is touched.

Technique mined from Shane's peace app (the hard-won bits, written fresh here):
- drop the parent harness's CLAUDE_CODE_* / CLAUDECODE env so the nested cc doesn't
  think it's a child session;
- sync OAuth creds into an isolated CLAUDE_CONFIG_DIR before each spawn (the isolated
  copy goes stale ~8h and 401s otherwise);
- 64MB stdout readline limit (stream-json emits one big JSON line per event);
- --session-id (new) / --resume (existing transcript) for durable per-thread sessions;
- neutral workdir + --setting-sources user so it doesn't absorb a project CLAUDE.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

CLAUDE = os.environ.get("ARGUS_CLAUDE_BIN",
                        os.path.expanduser("~/.npm-global/bin/claude"))
CONFIG_DIR = os.environ.get("ARGUS_CC_CONFIG_DIR", os.path.expanduser("~/argus/.cchome"))
WORKDIR = os.environ.get("ARGUS_CC_WORKDIR", os.path.expanduser("~/argus/.ccwork"))
SESSIONS_FILE = Path(os.path.expanduser("~/argus/.cc_sessions.json"))
IDLE_TIMEOUT = float(os.environ.get("ARGUS_CC_IDLE", "1800"))   # evict sessions idle > 30 min
_WORKDIR_SLUG = WORKDIR.replace("/", "-")


def _sync_credentials() -> None:
    src = Path(os.path.expanduser("~/.claude")) / ".credentials.json"
    dst = Path(CONFIG_DIR) / ".credentials.json"
    with contextlib.suppress(Exception):
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)


def _clean_env() -> dict:
    drop = ("CLAUDECODE", "CLAUDE_EFFORT", "AI_AGENT", "MEMORY_PRESSURE_WATCH")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE") and k not in drop}
    env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR
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
    return (Path(CONFIG_DIR) / "projects" / _WORKDIR_SLUG / f"{sid}.jsonl").is_file()


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
                "stream-json", "--verbose", *sid_args, "--setting-sources", "user",
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
            t = evt.get("type")
            if t == "system" and evt.get("subtype") == "init":
                self.session_id = evt.get("session_id", self.session_id)
                _save_sid(self.cid, self.session_id)
            elif t == "assistant":
                for block in evt.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        if self._q:
                            await self._q.put(("text", block["text"]))
                    elif block.get("type") == "tool_use" and self._q:
                        await self._q.put(("tool", block.get("name", "tool")))
            elif t == "result":
                if self._q:
                    await self._q.put(("done", evt.get("is_error", False)))

    async def send(self, text: str):
        """Yield cumulative assistant text for this turn (loop.stream_run contract).
        Yields ('__tool__', name) markers too so the server can drive the status pill."""
        async with self._lock:
            await self._ensure()
            self.last_used = time.monotonic()
            self._q = asyncio.Queue()
            payload = {"type": "user",
                       "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
            self.proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self.proc.stdin.drain()
            acc = ""
            while True:
                kind, data = await self._q.get()
                if kind == "text":
                    acc += ("\n\n" if acc else "") + data
                    yield acc
                elif kind == "tool":
                    yield ("__tool__", data)
                elif kind == "done":
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


async def send(conversation_id: str, text: str):
    """Module entry point: stream a turn through the conversation's persistent session."""
    await _evict_idle()
    s = _SESSIONS.get(conversation_id)
    if s is None:
        s = _SESSIONS[conversation_id] = CCSession(conversation_id)
    async for chunk in s.send(text):
        yield chunk
