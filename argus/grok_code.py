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
# High turn cap so multi-step jobs (scan/delete/build) aren't cut mid-task.
MAX_TURNS = int(os.environ.get("ARGUS_GROK_MAX_TURNS", "80"))
IDLE_TIMEOUT = float(os.environ.get("ARGUS_GROK_IDLE", "7200"))  # drop idle sid after 2h
# 0 = no whole-turn wall clock. A live grok process is allowed to run until it
# finishes (or the user cancels). Only a *dead* resume (no first event) is aborted.
TURN_TIMEOUT = float(os.environ.get("ARGUS_GROK_TURN_TIMEOUT", "0"))
FIRST_EVENT_TIMEOUT = float(os.environ.get("ARGUS_GROK_FIRST_TIMEOUT", "45"))
# 0 = wait for the previous turn to finish; never steal/kill it.
# Set ARGUS_GROK_STEAL=1 to restore the old "kill after 2s and take over" behavior.
LOCK_WAIT = float(os.environ.get("ARGUS_GROK_LOCK_WAIT", "0"))
STEAL = os.environ.get("ARGUS_GROK_STEAL", "").strip() in ("1", "true", "yes")
# While tools run, stdout can go quiet for a long time. Heartbeat this often so
# Argus/UI know the process is still alive.
ALIVE_POLL = float(os.environ.get("ARGUS_GROK_ALIVE_POLL", "15"))


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
        self._proc: asyncio.subprocess.Process | None = None

    def _forget_session(self) -> None:
        _clear_sid(self.cid)
        self.session_id = None

    def _cmd(self, *, prompt: str | None = None, prompt_file: str | None = None,
             force_fresh: bool = False) -> list[str]:
        """Build `grok -p` or multimodal `grok --prompt-file` (ACP JSON on disk).

        Never pass multi‑MB --prompt-json on argv — phone photos blow ARG_MAX
        (\"Argument list too long\"). Grok accepts ACP content-block JSON via
        --prompt-file when the path ends in .json.
        """
        args = [GROK]
        if prompt_file is not None:
            args.extend(["--prompt-file", prompt_file])
        else:
            args.extend(["-p", prompt if prompt is not None else "(empty message)"])
        args.extend([
            "--output-format", "streaming-messages-json",
            "--include-partial-messages",
            "--permission-mode", "bypassPermissions",
            "--max-turns", str(MAX_TURNS),
            "--cwd", WORKDIR,
        ])
        if self.session_id and not force_fresh:
            args.extend(["--resume", self.session_id])
        else:
            # Pin a fresh UUID so we control the id even if init is missed.
            sid = str(uuid.uuid4())
            args.extend(["--session-id", sid])
            self.session_id = sid
            _save_sid(self.cid, sid)
        return args

    async def _kill_active(self) -> None:
        """Terminate the in-flight grok process group (if any)."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            # start_new_session=True → killpg via negative pid
            if proc.pid:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, 15)
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                if proc.pid:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, 9)
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
        self._proc = None

    async def send(self, text: str, attachments=None):
        """Yield cumulative assistant text + ('__event__', ...) like claude_code.

        `attachments` may be:
          - raw UI list [{filename, isImage, dataUrl}] (materialised here), or
          - already-materialised list of argus.attachments.Attachment
        Images: ACP blocks via --prompt-file (compressed); not Anthropic source shape.

        On a wedged `--resume` (no first event), drops the session id and **retries
        once fresh** so chat recovers without a manual restart.
        """
        if not oauth_ready():
            raise RuntimeError(
                "Grok SuperGrok OAuth not signed in. On this host run: "
                "`grok login` (browser OAuth) → writes ~/.grok/auth.json. "
                "No XAI_API_KEY / console credits needed.")
        if not GROK or not Path(GROK).exists():
            raise RuntimeError(f"grok binary not found ({GROK!r}); install Grok Build CLI")

        # Default: wait for the previous turn to finish (don't abort a live task).
        # ARGUS_GROK_STEAL=1 restores the old "kill after LOCK_WAIT and take over".
        if STEAL:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=LOCK_WAIT or 2.0)
            except asyncio.TimeoutError:
                await self._kill_active()
                await self._lock.acquire()
        else:
            await self._lock.acquire()

        prompt_path: Path | None = None
        try:
            self.last_used = time.monotonic()
            Path(WORKDIR).mkdir(parents=True, exist_ok=True)

            from argus import attachments as attmod
            mats = attachments or []
            if mats and not hasattr(mats[0], "path"):
                mats = attmod.materialize(mats, conversation_id=self.cid)

            prompt = text or "(empty message)"
            if mats:
                prompt_path = attmod.write_acp_prompt_file(text or "", mats)

            # Attempt 1: resume if we have a sid. On first-event timeout, attempt 2
            # is always force_fresh (no --resume).
            for attempt, force_fresh in enumerate((False, True)):
                if mats:
                    cmd = self._cmd(prompt_file=str(prompt_path), force_fresh=force_fresh)
                else:
                    cmd = self._cmd(prompt=prompt, force_fresh=force_fresh)
                used_resume = any(a == "--resume" for a in cmd)
                try:
                    async for chunk in self._run_cmd(cmd, used_resume=used_resume):
                        yield chunk
                    return  # success
                except TimeoutError as e:
                    msg = str(e)
                    # Only auto-retry when the *first* attempt used resume and
                    # failed before/at first output — classic wedged session.
                    if attempt == 0 and used_resume and "no output" in msg:
                        self._forget_session()
                        continue
                    raise
        finally:
            if prompt_path is not None:
                with contextlib.suppress(Exception):
                    prompt_path.unlink(missing_ok=True)
            self._lock.release()

    async def _run_cmd(self, cmd: list[str], *, used_resume: bool):
        """Spawn one `grok` and stream parsed events until done/timeout."""
        from .stream_util import merge_stream_text

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=WORKDIR, env=_clean_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=2 ** 26,
            start_new_session=True,  # own process group → killpg on cancel
        )
        self._proc = proc
        acc = ""
        stderr_buf: list[bytes] = []

        async def _drain_err():
            while proc.stderr:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.append(chunk)

        err_task = asyncio.create_task(_drain_err())
        t0 = time.monotonic()
        saw_event = False
        # Coalesce thinking_delta tokens (~token-rate) into ~4Hz events so the
        # SSE + activity panel don't melt under thousands of tiny publishes.
        think_buf = ""
        think_last_emit = 0.0
        THINK_INTERVAL = 0.25

        async def _flush_think(force: bool = False):
            nonlocal think_buf, think_last_emit
            if not think_buf:
                return
            now = time.monotonic()
            if not force and (now - think_last_emit) < THINK_INTERVAL:
                return
            chunk = think_buf
            think_buf = ""
            think_last_emit = now
            yield ("__event__", {"kind": "thinking", "text": chunk})

        try:
            assert proc.stdout is not None
            while True:
                elapsed = time.monotonic() - t0
                if TURN_TIMEOUT > 0 and elapsed >= TURN_TIMEOUT:
                    raise TimeoutError(
                        f"Grok turn exceeded {TURN_TIMEOUT:.0f}s wall-clock")
                if not saw_event:
                    line_timeout = max(1.0, FIRST_EVENT_TIMEOUT - elapsed)
                else:
                    # Tools can go quiet for minutes. Poll aliveness; don't abort
                    # a live process. Heartbeat so the UI stays "working".
                    line_timeout = ALIVE_POLL
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=line_timeout)
                except asyncio.TimeoutError:
                    if not saw_event:
                        raise TimeoutError(
                            f"Grok produced no output in {FIRST_EVENT_TIMEOUT:.0f}s"
                            + (" (resume may be wedged)" if used_resume else ""))
                    # Mid-turn silence: if grok is still running, keep going.
                    if proc.returncode is None:
                        yield ("__event__", {
                            "kind": "heartbeat",
                            "text": f"still working ({int(elapsed)}s)",
                        })
                        continue
                    break
                if not line:
                    break
                saw_event = True
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
                        async for t in _flush_think(force=True):
                            yield t
                        piece = data if isinstance(data, str) else str(data)
                        if piece and acc and piece.startswith(acc):
                            acc = piece
                        else:
                            acc = acc + piece
                        yield acc
                    elif kind == "text":
                        async for t in _flush_think(force=True):
                            yield t
                        acc = merge_stream_text(
                            acc, data if isinstance(data, str) else str(data)
                        )
                        yield acc
                    elif kind == "event":
                        if isinstance(data, dict) and data.get("kind") == "thinking":
                            think_buf += data.get("text") or ""
                            async for t in _flush_think(force=False):
                                yield t
                            continue
                        async for t in _flush_think(force=True):
                            yield t
                        # Tool-only phases leave the bubble blank → show a
                        # lightweight status line so the UI doesn't look frozen.
                        if (isinstance(data, dict) and data.get("kind") == "tool_use"
                                and not acc):
                            name = data.get("name") or "tool"
                            yield f"_(running `{name}`…)_"
                        yield ("__event__", data)
                    elif kind == "done":
                        async for t in _flush_think(force=True):
                            yield t
                        # max-turns is a *soft* stop — if the model still has
                        # unfinished work, say so in the bubble instead of
                        # looking like a silent stall.
                        subtype = data.get("subtype") or ""
                        if data.get("is_error") and "max_turn" in subtype:
                            extra = (
                                "\n\n_(Hit the turn cap — say “continue” to keep going.)_"
                            )
                            acc = (acc or "") + extra
                            yield acc
                        yield ("__event__", {"kind": "done",
                                            "duration_ms": data.get("duration_ms")})
                        if data.get("is_error") and not acc:
                            subtype = data.get("subtype") or "error"
                            raise RuntimeError(f"Grok turn failed ({subtype})")
            async for t in _flush_think(force=True):
                yield t
        except (TimeoutError, asyncio.CancelledError):
            await self._kill_active()
            if used_resume:
                self._forget_session()
            raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            if self._proc is proc:
                self._proc = None
            err_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await err_task

        rc = proc.returncode
        if rc not in (0, None) and not acc:
            err = b"".join(stderr_buf).decode(errors="replace").strip()
            if used_resume or "session" in err.lower() or "resume" in err.lower():
                self._forget_session()
            raise RuntimeError(err or f"grok exited {rc}")
        self.last_used = time.monotonic()


_SESSIONS: dict[str, GrokSession] = {}


async def _evict_idle() -> None:
    now = time.monotonic()
    for cid, s in list(_SESSIONS.items()):
        # Never evict a turn that's still running.
        if s._proc is not None and s._proc.returncode is None:
            continue
        if now - s.last_used > IDLE_TIMEOUT:
            await s._kill_active()
            _SESSIONS.pop(cid, None)


async def send(conversation_id: str, text: str, attachments=None):
    """Module entry: stream one SuperGrok turn for this conversation."""
    await _evict_idle()
    s = _SESSIONS.get(conversation_id)
    if s is None:
        s = _SESSIONS[conversation_id] = GrokSession(conversation_id)
    async for chunk in s.send(text, attachments=attachments):
        yield chunk
