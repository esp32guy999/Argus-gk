"""SUPER-SEE (SS) — CPU supervisory control system, v1.

Public surface:
    from argus.ss import observe, observe_task, Snapshot

v1 is shadow-only. SEE remains the authority. Rules handle facts;
Z-Engineer (think off) is the optional judgment head for leftover ambiguity.

The *worker* is model-agnostic: whichever model is in the harness seat is
supervised. The judgment head is a separate CPU process, never llama-swap.
"""
from .observe import enabled, observe, observe_task
from .protocol import SsDecision
from .snapshot import Snapshot, snapshot_from_task
from .model import WINNER, WINNER_PATH

__all__ = [
    "SsDecision",
    "Snapshot",
    "observe",
    "observe_task",
    "snapshot_from_task",
    "enabled",
    "WINNER",
    "WINNER_PATH",
]
