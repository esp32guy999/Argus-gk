"""Code-execution lane — let a model WRITE code and RUN it, sandboxed. EXPERIMENTAL.

Execution is high-blast, so it is confined to a bubblewrap sandbox (validated rootless):
  - host filesystem READ-ONLY (code cannot touch the real disk),
  - NO network (`--unshare-net` — no exfiltration, no reaching services),
  - an ephemeral tmpfs `/tmp` as cwd (scratch; gone when the call returns),
  - pid isolation + a hard timeout.
Returns ok/exit_code/stdout/stderr honestly (like the shell lane). Code is fed on stdin
(`python3 -` / `bash -s`) so there's no code file to bind past the read-only root.

Opt-in by config presence (config/run_code.yaml); gated per-model in loop.LANE_MODEL_GATES.
NO export: whatever the code writes lives in the ephemeral scratch and vanishes. A
human-gated export path (promote proven code out of the sandbox) is a separate, later
capability — deliberately not built here.
"""
from __future__ import annotations

import shutil
import subprocess

import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

# interpreter invoked to read the program from STDIN
_INTERP = {"python": ["python3", "-"], "bash": ["bash", "-s"]}


def tools(manifest_path: str = "config/run_code.yaml") -> list[Tool]:
    with open(manifest_path) as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("enabled", True):
        return []
    if not shutil.which("bwrap"):
        print("[run_code] bubblewrap (bwrap) not found — lane DISABLED (no safe sandbox).")
        return []

    timeout_s = float(cfg.get("timeout", 20))
    max_output = int(cfg.get("max_output_chars", 16000))
    langs = set(cfg.get("languages", ["python"]))

    def _trunc(s: str) -> str:
        return (s[:max_output] + f"\n...[truncated {len(s) - max_output} chars]"
                if len(s) > max_output else s)

    def run_code(code: str, language: str = "python") -> dict:
        """Write and RUN code in a locked-down sandbox, and return its output. The host
        filesystem is READ-ONLY, there is NO network, and a hard timeout applies — the
        code can compute and write to its own scratch dir but cannot touch the real
        system, reach the network, or run forever. Use to write-and-run a script (a
        calculation, a data transform, a quick check) and see the result. Returns ok,
        exit_code, stdout, stderr. `language`: 'python' (default) or 'bash'."""
        if not code or not code.strip():
            raise ModelRetry("run_code: no code provided.")
        if language not in langs:
            raise ModelRetry(f"run_code: language {language!r} not enabled. "
                             f"Options: {sorted(langs)}.")
        argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--unshare-net", "--unshare-pid", "--die-with-parent",
                "--chdir", "/tmp", "--", *_INTERP[language]]
        try:
            proc = subprocess.run(argv, input=code, capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired:
            raise ModelRetry(f"run_code: exceeded the {timeout_s:g}s timeout and was killed. "
                             "Make the code terminate quickly (no infinite loops or waits).")
        except FileNotFoundError as e:
            raise ModelRetry(f"run_code: sandbox/interpreter missing ({e}).")
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": _trunc(proc.stdout or ""),
            "stderr": _trunc(proc.stderr or ""),
            "sandbox": "bubblewrap: read-only host, no network, ephemeral scratch",
        }

    return [Tool(
        name="run_code",
        description=("Write and RUN code in a locked-down sandbox (read-only host, NO "
                     "network, hard timeout) and get its output — for computations, data "
                     "transforms, or quick scripts. Returns ok/exit_code/stdout/stderr. "
                     "language: python (default) or bash."),
        tags=["code", "run", "execute", "python", "script", "compute", "program", "eval"],
        func=run_code,
        provider="run_code",
        example={"code": "print(sum(range(10)))", "language": "python"},
    )]
