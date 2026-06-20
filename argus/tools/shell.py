"""Shell / CLI tool lane — execute system commands on the Argus host.

THE HIGH-BLAST-RADIUS LANE. The spec originally excluded a shell lane on purpose;
it exists now by explicit owner decision (hazards acknowledged). Because of that,
the value is *entirely* in the guardrails — layered defense in depth:

  1. Opt-in by config presence — no manifest -> lane absent (like every other lane).
  2. allowlist — if set, ONLY these binaries may run. Empty = all allowed (owner's call).
  3. denylist — catastrophic patterns are always refused (rm -rf /, dd to a block
     device, mkfs, fork bombs, shutdown...). Defense in depth, NOT the primary
     boundary — the allowlist + owner trust is.
  4. shell=false (default) runs argv directly (no pipes/globs/redirects) so the
     allowlist is STRICTLY enforceable on argv[0]. shell=true enables a real shell
     (pipes work) at the cost of best-effort, per-segment allowlisting.
  5. timeout + output truncation + fixed cwd — none of them model-controllable, so
     the model only ever passes `command` (also dodges the optional-param grammar loop).

Non-zero exit codes are RETURNED (the model reads stderr and adapts); only spawn
failures, timeouts, and policy refusals raise ModelRetry (teaching errors).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

# Catastrophic patterns refused regardless of allowlist. Defense in depth ONLY —
# the real boundary is the allowlist + owner trust. Kept tight so ordinary
# destructive ops (e.g. `rm -rf ./build`) still work; only host-wiping does not.
_DEFAULT_DENY: list[tuple[str, str]] = [
    # recursive+force delete aimed at root, home, or a bare glob
    ("recursive root/home delete",
     r"\brm\b[^|;&]*\s-\S*[rf]\S*\b[^|;&]*\s(/|/\*|~|\$HOME|\*)(\s|$)"),
    ("filesystem format",          r"\b(mkfs\S*|wipefs|mkswap)\b"),
    ("write to block device",      r"(\bdd\b[^|;&]*\bof=|>\s*)/dev/(sd|nvme|mmcblk|vd|hd)"),
    ("fork bomb",                  r":\s*\(\s*\)\s*\{.*:\s*\|\s*:.*\}\s*;\s*:"),
    ("power state change",         r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b"),
]


def _commands_in(command: str) -> list[str]:
    """Best-effort: the binary at each command position (start + after | ; & && ||).
    Used for allowlist enforcement when shell=true (where argv[0] alone isn't enough)."""
    bins: list[str] = []
    for seg in re.split(r"\|\||&&|[|;&\n]", command):
        try:
            parts = shlex.split(seg)
        except ValueError:
            parts = seg.split()
        i = 0
        while i < len(parts) and re.match(r"^\w+=", parts[i]):  # skip VAR=val env prefixes
            i += 1
        if i < len(parts):
            bins.append(os.path.basename(parts[i]))
    return bins


def tools(manifest_path: str = "config/shell_tools.yaml") -> list[Tool]:
    """Provider entry point: emit the (single) guarded shell tool in the uniform contract.
    Returns [] if the lane is disabled in config."""
    with open(manifest_path) as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("enabled", True):
        return []

    use_shell  = bool(cfg.get("shell", False))
    timeout_s  = float(cfg.get("timeout", 30))
    max_output = int(cfg.get("max_output_chars", 16000))
    cwd        = os.path.expanduser(str(cfg.get("cwd", "~")))
    allowlist  = set(cfg.get("allowlist") or [])             # empty = all binaries allowed
    deny = list(_DEFAULT_DENY) + [("custom", p) for p in (cfg.get("denylist") or [])]
    deny_compiled = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in deny]

    def _truncate(s: str) -> tuple[str, bool]:
        if len(s) > max_output:
            return s[:max_output] + f"\n...[truncated {len(s) - max_output} chars]", True
        return s, False

    def run_command(command: str) -> dict:
        """Run a shell command on the Argus host and return its exit code, stdout and stderr.
        Use for system/homelab operations: status checks, service queries, file inspection.
        Prefer specific, read-only commands. The return is a dict — a non-zero exit_code
        means the command failed, so read stderr and adjust rather than repeating it."""
        if not command or not command.strip():
            raise ModelRetry("shell: empty command. Provide a command to run.")

        for name, rx in deny_compiled:
            if rx.search(command):
                raise ModelRetry(
                    f"shell: refused — command matches the '{name}' safety denylist and is "
                    "blocked outright. Rephrase to a safe, specific operation."
                )

        if allowlist:
            bins = _commands_in(command)
            bad = [b for b in bins if b not in allowlist]
            if not bins or bad:
                raise ModelRetry(
                    f"shell: {', '.join(bad) or 'command'} not in the allowlist. "
                    f"Permitted: {', '.join(sorted(allowlist))}."
                )

        if use_shell:
            argv: str | list[str] = command
        else:
            try:
                argv = shlex.split(command)
            except ValueError as e:
                raise ModelRetry(f"shell: could not parse command ({e}). Check your quoting.")
            if not argv:
                raise ModelRetry("shell: empty command after parsing.")

        try:
            proc = subprocess.run(
                argv, shell=use_shell, cwd=cwd, capture_output=True,
                text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise ModelRetry(
                f"shell: command exceeded the {timeout_s:g}s timeout and was killed. "
                "Run something shorter, or background long-running jobs yourself."
            )
        except FileNotFoundError as e:
            raise ModelRetry(f"shell: command not found ({e}). Check the binary name / PATH.")

        out, out_tr = _truncate(proc.stdout or "")
        err, err_tr = _truncate(proc.stderr or "")
        return {
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "truncated": out_tr or err_tr,
        }

    return [
        Tool(
            name=cfg.get("tool_name", "run_command"),
            description=cfg.get(
                "description",
                "Execute a shell command on the Argus host (guarded). "
                "Returns exit_code, stdout, stderr.",
            ),
            tags=cfg.get("tags", ["shell", "cli", "system", "command", "exec"]),
            func=run_command,
            provider="shell",
            example=cfg.get("example", {"command": "uname -a"}),
        )
    ]
