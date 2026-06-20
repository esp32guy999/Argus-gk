"""Anti-stall watchdog. Signals double as Prometheus metrics.

v1: repeated-call detection (here) + turn budget (enforced in loop.py via the
engine's UsageLimits). No-progress detection is stubbed for a later pass.
NO per-tool armed/unarmed state machine — the turn boundary gives atomicity.
"""
from __future__ import annotations

import json

from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelRetry

from . import metrics


class RepeatedCallDetector:
    """Flags identical (tool, args) calls so the loop driver can intervene
    instead of spinning. Cheap in-memory counter, scoped per task."""

    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self._seen: dict[str, int] = {}

    def record(self, tool: str, args: str) -> bool:
        """Return True if this exact call has now hit the repeat limit."""
        key = f"{tool}:{args}"
        self._seen[key] = self._seen.get(key, 0) + 1
        if self._seen[key] >= self.limit:
            metrics.LOOP_DETECTED.inc()
            return True
        return False


def make_capability(repeat_limit: int = 2) -> Hooks:
    """Build a fresh per-run watchdog as a Pydantic AI capability.

    Intervenes when the model repeats an identical (tool, args) call — it's
    looping. We raise ModelRetry so the model gets a corrective nudge instead of
    re-running the same dead-end. A NEW detector per call = per-task scope.
    Pass to Agent(capabilities=[make_capability()]).
    """
    detector = RepeatedCallDetector(limit=repeat_limit)

    def before_tool_execute(ctx, *, call, tool_def, args):
        key = json.dumps(args, sort_keys=True, default=str)
        if detector.record(tool_def.name, key):
            raise ModelRetry(
                f"You have already called '{tool_def.name}' with these exact "
                f"arguments and it did not move the task forward. Use different "
                f"arguments, a different tool, or give your final answer."
            )
        return args

    return Hooks(before_tool_execute=before_tool_execute)
