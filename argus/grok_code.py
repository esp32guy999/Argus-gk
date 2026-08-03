"""SuperGrok OAuth path for Argus — same role as claude_code.py, different CLI.

Spawns `grok -p` (Grok Build) per turn, resuming a durable session id per
conversation. Auth is the user's SuperGrok / X Premium+ OAuth in
`~/.grok/auth.json` — **not** `XAI_API_KEY` / console.x.ai credits.

Lessons borrowed from claude_code + the OAuth post-mortem:
- Use the canonical `~/.grok` home; never copy tokens into an isolated dir.
- Strip API-key env vars from the child so the CLI cannot fall back to a
  zero-credit console key and 403.
- One session id per conversation; resume across turns (Grok owns history).
- Stream `streaming-messages-json` (+ partials) into the same bubble contract
  as Claude Code (`text` + `__event__` activity markers).

NOTE: with cwd=~/argus and --permission-mode bypassPermissions, Grok can
read/write the tree (and more) via its built-in tools — same class of power
as the nested Claude Code session.
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

GROK = os.environ.get("ARGUS_GROK_BIN") or shutil.which("grok") or os.path.expanduser("~/.local/bin/grok")
WORKDIR = os.environ.get("ARGUS_GROK_WORKDIR", os.path.expanduser("~/argus"))
AUTH_FILE = Path(os.environ.get("ARGUS_GROK_AUTH", os.path.expanduser("~/.grok/auth.json")))
SESSIONS_FILE = Path(os.environ.get("ARGUS_GROK_SESSIONS",
                                    os.path.expanduser("~/argus/.grok_sessions.json")))
MAX_TURNS = int(os.environ.get("ARGUS_GROK_MAX_TURNS", "24"))
IDLE_TIMEOUT = float(os.environ.get("ARGUS_GROK_IDLE", "1800"))  # drop sid after 30m idle


def oauth_ready() -> bool:
    """True when ~/.grok/auth.json has an OIDC SuperGrok/X session."""
    try:
        data = json.loads(AUTH_FILE.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for v in data.values():
        if isinstance(v, dict) and v.get("auth_mode") in ("oidc", "oauth"):
            return True
        # older shape: any entry with a refresh_token counts
        if isinstance(v, dict) and v.get("refresh_token"):
            return True
    return False


def _clean_env() -> dict:
    """Child env: keep PATH/HOME so OAuth is found; drop API keys so OAuth wins."""
    env = dict(os.environ)
    for k in ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "XAI_KEY"):
        env.pop(k, None)
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


def _clear_sid(cid: str) -> None:
    sids = _load_sids()
    if cid in sids:
        sids.pop(cid, None)
        with contextlib.suppress(Exception):
            SESSIONS_FILE.write_text(json.dumps(sids))


def _parse_event(evt: dict) -> list[tuple]:
    """Map one streaming-messages-json line to (kind, data) items.

    kinds:
      ("text_delta", str)  — incremental assistant text
      ("text", str)        — whole text block (fallback)
      ("event", dict)      — activity markers for the UI
      ("done", dict)       — turn finished
      ("session", str)     — session id from system/init
    """
    out: list[tuple] = []
    t = evt.get("type")

    if t == "system" and evt.get("subtype") == "init":
        sid = evt.get("session_id")
        if sid:
            out.append(("session", sid))
        return out

    if t == "stream_event":
        e = evt.get("event") or {}
        et = e.get("type")
        if et == "content_block_delta":
            d = e.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and d.get("text"):
                out.append(("text_delta", d["text"]))
            elif dt == "thinking_delta" and d.get("thinking"):
                out.append(("event", {"kind": "thinking", "text": d["thinking"]}))
        elif et == "content_block_start":
            cb = e.get("content_block") or {}
            if cb.get("type") == "tool_use":
                out.append(("event", {"kind": "tool_use",
                                      "name": cb.get("name") or "tool",
                                      "input": cb.get("input") or {}}))
        return out

    if t == "assistant":
        for block in evt.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text" and (block.get("text") or "").strip():
                out.append(("text", block["text"]))
            elif bt == "thinking" and (block.get("thinking") or "").strip():
                out.append(("event", {"kind": "thinking", "text": block["thinking"]}))
            elif bt == "tool_use":
                out.append(("event", {"kind": "tool_use",
                                      "name": block.get("name") or "tool",
                                      "input": block.get("input") or {}}))
        return out

    if t == "user":
        for block in evt.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                out.append(("event", {"kind": "tool_result", "text": text[:2000]}))
        return out

    if t == "result":
        out.append(("done", {"is_error": bool(evt.get("is_error")),
                             "duration_ms": evt.get("duration_ms"),
                             "subtype": evt.get("subtype")}))
        return out

    return out


class GrokSession:
    """One SuperGrok-backed conversation (session id + per-turn subprocess)."""

    def __init__(self, cid: str):
        self.cid = cid
        self.session_id: str | None = _load_sids().get(cid)
        self._lock = asyncio.Lock()
        self.last_used = time.monotonic()

    def _cmd(self, *, prompt: str | None = None, prompt_json: str | None = None) -> list[str]:
        """Build `grok -p` or `grok --prompt-json` argv (mutually exclusive prompt modes)."""
        args = [GROK]
        if prompt_json is not None:
            # Multimodal / structured content blocks (images + text) — see attachments.vision_content_blocks
            args.extend(["--prompt-json", prompt_json])
        else:
            args.extend(["-p", prompt if prompt is not None else "(empty message)"])
        args.extend([
            "--output-format", "streaming-messages-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(MAX_TURNS),
            "--cwd", WORKDIR,
        ])
        if self.session_id:
            args.extend(["--resume", self.session_id])
        else:
            # Pin a fresh UUID so we control the id even if init is missed.
            sid = str(uuid.uuid4())
            args.extend(["--session-id", sid])
            self.session_id = sid
            _save_sid(self.cid, sid)
        return args

    async def send(self, text: str, attachments=None):
        """Yield cumulative assistant text + ('__event__', ...) like claude_code.

        `attachments` may be:
          - raw UI list [{filename, isImage, dataUrl}] (materialised here), or
          - already-materialised list of argus.attachments.Attachment
        Images go through --prompt-json as Anthropic-style content blocks.
        """
        if not oauth_ready():
            raise RuntimeError(
                "Grok SuperGrok OAuth not signed in. On this host run: "
                "`grok login` (browser OAuth) → writes ~/.grok/auth.json. "
                "No XAI_API_KEY / console credits needed.")
        if not GROK or not Path(GROK).exists():
            raise RuntimeError(f"grok binary not found ({GROK!r}); install Grok Build CLI")

        async with self._lock:
            self.last_used = time.monotonic()
            Path(WORKDIR).mkdir(parents=True, exist_ok=True)

            from argus import attachments as attmod
            mats = attachments or []
            if mats and not hasattr(mats[0], "path"):
                # raw UI dicts
                mats = attmod.materialize(mats, conversation_id=self.cid)

            prompt_json = None
            prompt = text or "(empty message)"
            if mats:
                blocks = attmod.vision_content_blocks(text or "", mats)
                prompt_json = json.dumps(blocks)
                cmd = self._cmd(prompt_json=prompt_json)
            else:
                cmd = self._cmd(prompt=prompt)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=WORKDIR, env=_clean_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2 ** 26,
            )
            acc = ""
            saw_delta = False
            stderr_buf: list[bytes] = []

            async def _drain_err():
                while proc.stderr:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_buf.append(chunk)

            err_task = asyncio.create_task(_drain_err())
            try:
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    line = line.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for kind, data in _parse_event(evt):
                        if kind == "session":
                            self.session_id = data
                            _save_sid(self.cid, data)
                        elif kind == "text_delta":
                            saw_delta = True
                            acc += data
                            yield acc
                        elif kind == "text":
                            # Whole block: replace if we never saw deltas (or it's longer)
                            if not saw_delta or len(data) >= len(acc):
                                acc = data
                            else:
                                acc = (acc + "\n\n" + data) if acc else data
                            yield acc
                        elif kind == "event":
                            yield ("__event__", data)
                        elif kind == "done":
                            yield ("__event__", {"kind": "done",
                                                "duration_ms": data.get("duration_ms")})
                            if data.get("is_error") and not acc:
                                subtype = data.get("subtype") or "error"
                                raise RuntimeError(f"Grok turn failed ({subtype})")
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                err_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await err_task

            rc = proc.returncode
            if rc not in (0, None) and not acc:
                err = b"".join(stderr_buf).decode(errors="replace").strip()
                # Stale resume → drop sid so the next turn starts fresh
                if "session" in err.lower() or "resume" in err.lower():
                    _clear_sid(self.cid)
                    self.session_id = None
                raise RuntimeError(err or f"grok exited {rc}")
            self.last_used = time.monotonic()


_SESSIONS: dict[str, GrokSession] = {}


async def _evict_idle() -> None:
    now = time.monotonic()
    for cid, s in [(c, x) for c, x in _SESSIONS.items() if now - x.last_used > IDLE_TIMEOUT]:
        _SESSIONS.pop(cid, None)


async def send(conversation_id: str, text: str, attachments=None):
    """Module entry: stream one SuperGrok turn for this conversation."""
    await _evict_idle()
    s = _SESSIONS.get(conversation_id)
    if s is None:
        s = _SESSIONS[conversation_id] = GrokSession(conversation_id)
    async for chunk in s.send(text, attachments=attachments):
        yield chunk
