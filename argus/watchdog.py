"""Anti-stall watchdog. Signals double as Prometheus metrics.

v1: repeated-call detection (here) + turn budget (enforced in loop.py via the
engine's UsageLimits). No-progress detection is stubbed for a later pass.
NO per-tool armed/unarmed state machine — the turn boundary gives atomicity.
"""
from __future__ import annotations
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
