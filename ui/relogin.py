"""Remote Claude Code re-login — drive the real `claude auth login` under a PTY.

Anthropic periodically revokes the OAuth grant, forcing an interactive /login.
This lets that login be completed from a phone via the Argus canvas:

  start()        spawns `claude auth login`, scrapes the hosted
                 (platform.claude.com) authorize URL it prints, and parks the
                 process at its "Paste code here" prompt.
  submit_code()  feeds the pasted code to that SAME process's stdin (the PKCE
                 verifier lives inside it, so it must be the same process),
                 waits for the token exchange to rewrite ~/.claude/.credentials
                 .json, then restarts claude-shim.

Nothing here mints a token on its own — Anthropic still requires the human
browser approval. We only relay the URL out and the code back in. The CLI is
the source of truth for the OAuth dance, so this survives Anthropic changing
endpoints/clients (no reverse-engineered constants).
"""
from __future__ import annotations
import os
import re
import pty
import json
import time
import select
import signal
import secrets
import subprocess
import threading

# `claude` is not on the systemd --user service PATH; resolve it explicitly.
CLAUDE_BIN = next((p for p in (
    "/home/shane/.npm-global/bin/claude",
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/usr/local/bin/claude",
) if os.path.exists(p)), "claude")

# PATH for spawned children (service env lacks the npm/local bins).
_SUB_PATH = ":".join([
    "/home/shane/.npm-global/bin",
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin", "/usr/bin", "/bin",
])

# Strip ANSI/VT noise (incl. OSC-8 hyperlink wrappers and CRs) so we can regex the URL.
_ANSI = re.compile(
    rb'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][0-9;]*(?:\x07|\x1b\\)?|\x1b[()][AB0-2]|\r')
_URL = re.compile(r'https://claude\.com/cai/oauth/authorize\?[^\s"\'\x1b\x07]+')

LOGIN_TTL = 600          # reap an unfinished attempt after 10 min
SHIM_UNIT = "claude-shim"


def _clean(buf: bytes) -> str:
    return _ANSI.sub(b'', buf).decode("utf-8", "replace")


class LoginManager:
    def __init__(self):
        self._sessions: dict[str, dict] = {}   # sid -> {proc, fd, buf, url, created}
        self._lock = threading.Lock()

    def _kill(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if not s:
            return
        t = s.get("timer")
        if t:
            t.cancel()
        try:
            s["proc"].kill()
        except Exception:
            pass
        try:
            os.close(s["fd"])
        except Exception:
            pass

    def _reap(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._sessions.items()):
            if now - s["created"] > LOGIN_TTL:
                self._kill(sid)

    @staticmethod
    def sweep_orphans() -> None:
        """Kill parked `claude auth login` procs left by a previous server life.

        They sit forever waiting on stdin we can no longer reach (the session
        dict that tracked them died with the old process). Matched by argv
        STRUCTURE (argv0 ends in `claude`, argv1/2 == auth/login), not a loose
        `pkill -f` regex — so a shell or editor that merely mentions the string
        can't be caught in the blast radius.
        """
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                argv = [a for a in open(f"/proc/{pid}/cmdline", "rb")
                        .read().decode("utf-8", "replace").split("\x00") if a]
            except Exception:
                continue
            if len(argv) >= 3 and argv[0].endswith("claude") \
                    and argv[1] == "auth" and argv[2] == "login":
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception:
                    pass

    def shutdown(self) -> None:
        with self._lock:
            for sid in list(self._sessions):
                self._kill(sid)

    def start(self, timeout: float = 30) -> dict:
        """Spawn the login, return {session_id, url}. Does NOT touch credentials."""
        with self._lock:
            self._reap()
            for sid in list(self._sessions):   # only one pending attempt at a time
                self._kill(sid)

            master, slave = pty.openpty()
            env = dict(os.environ)
            env["BROWSER"] = "/bin/true"        # suppress real browser; use the printed URL
            env["TERM"] = "dumb"
            env["PATH"] = _SUB_PATH
            for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
                env.pop(k, None)                # never let a stray key hijack the OAuth login
            proc = subprocess.Popen(
                [CLAUDE_BIN, "auth", "login", "--claudeai"],
                stdin=slave, stdout=slave, stderr=slave,
                env=env, close_fds=True, start_new_session=True)
            os.close(slave)

            buf = b""
            url = None
            end = time.monotonic() + timeout
            while time.monotonic() < end and url is None:
                r, _, _ = select.select([master], [], [], 0.5)
                if master in r:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    for m in _URL.finditer(_clean(buf)):
                        if "localhost" not in m.group(0):   # want the paste (hosted) URL
                            url = m.group(0)
                            break
                elif proc.poll() is not None:
                    break

            if not url:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    os.close(master)
                except Exception:
                    pass
                raise RuntimeError(
                    "claude auth login did not emit an auth URL. tail:\n"
                    + _clean(buf)[-500:])

            sid = secrets.token_urlsafe(8)
            # Watchdog: kill an abandoned login after TTL even if start() is
            # never called again (the only other reap trigger).
            timer = threading.Timer(LOGIN_TTL, self._kill, args=(sid,))
            timer.daemon = True
            timer.start()
            self._sessions[sid] = dict(proc=proc, fd=master, buf=buf,
                                       url=url, created=time.monotonic(), timer=timer)
            return {"session_id": sid, "url": url}

    def submit_code(self, sid: str, code: str, timeout: float = 45) -> dict:
        """Feed the pasted code to the parked login, then verify + restart the shim."""
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                raise RuntimeError("no active login session (expired — tap Start again)")
            os.write(s["fd"], (code.strip() + "\r").encode())
            buf = s["buf"]
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if s["proc"].poll() is not None:
                    break
                r, _, _ = select.select([s["fd"]], [], [], 0.5)
                if s["fd"] in r:
                    try:
                        chunk = os.read(s["fd"], 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
            rc = s["proc"].poll()
            tail = _clean(buf)[-400:]
            self._kill(sid)

        status = self.auth_status()             # outside the lock (spawns a child)
        shim = self.restart_shim() if status.get("loggedIn") else {"skipped": "not logged in"}
        return {"exit_code": rc, "loggedIn": status.get("loggedIn", False),
                "status": status, "shim": shim, "tail": tail}

    def auth_status(self) -> dict:
        try:
            env = dict(os.environ)
            env["PATH"] = _SUB_PATH
            r = subprocess.run([CLAUDE_BIN, "auth", "status"],
                               capture_output=True, text=True, timeout=20, env=env)
            return json.loads(r.stdout.strip() or "{}")
        except Exception as e:
            return {"error": str(e), "loggedIn": False}

    def restart_shim(self) -> dict:
        try:
            r = subprocess.run(["systemctl", "--user", "restart", SHIM_UNIT],
                               capture_output=True, text=True, timeout=30)
            return {"ok": r.returncode == 0, "stderr": r.stderr.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}


manager = LoginManager()
