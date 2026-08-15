"""Deterministic SS watchdog — facts first, model only for leftover ambiguity.

Activity is not progress. Real evidence + objective movement wins over
repeated tools. Inference/resource failures are hard facts.
"""
from __future__ import annotations

import os

from .protocol import SsDecision
from .snapshot import Snapshot

REPEAT_N = int(os.environ.get("ARGUS_SS_REPEAT_N", "3"))
STALL_SEC = float(os.environ.get("ARGUS_SS_STALL_SEC", "20"))
HIGH_CALLS = int(os.environ.get("ARGUS_SS_HIGH_CALLS", "8"))
LONG_TASK_SEC = float(os.environ.get("ARGUS_SS_LONG_SEC", "120"))
SOFT_STALL_SEC = float(os.environ.get("ARGUS_SS_SOFT_STALL_SEC", "5"))


def _no_new_info(snap: Snapshot) -> bool:
    return (not snap.progress.new_evidence) and snap.progress.objective_progress <= 0


def apply_rules(snap: Snapshot) -> SsDecision | None:
    """Return a decision when the fact is unambiguous; None if ambiguous."""
    # Resource facts — do not wait for a model.
    if not snap.system.backend_ok:
        return SsDecision("ASK_USER", "RESOURCE_FAILURE", source="rules",
                          details={"service": snap.system.service})
    if not snap.system.gpu_ok:
        return SsDecision("WAIT", "RESOURCE_FAILURE", source="rules")

    # Inference stall beats leftover progress numbers — tokens have stopped.
    # Require a real token stream so non-streaming run()/run_async cannot fake it.
    if (snap.model.generating and snap.model.streamed
            and snap.model.seconds_since_token >= STALL_SEC):
        return SsDecision("WAIT", "INFERENCE_STALL", source="rules")

    # Real progress: never interrupt (false-positive-repeat case).
    if snap.progress.new_evidence and snap.progress.objective_progress > 0:
        return SsDecision("NO_OP", "PRODUCTIVE_PROGRESS", source="rules")

    no_info = _no_new_info(snap)

    # Identical tool spam with no new information.
    if snap.tools.repeated_calls >= REPEAT_N and no_info:
        return SsDecision("REPLAN", "REPEATED_TOOL_CALL", source="rules",
                          details={"last_tool": snap.tools.last_tool,
                                   "repeated_calls": snap.tools.repeated_calls})

    # Busy, zero objective movement, no new evidence.
    if snap.tools.calls >= HIGH_CALLS and no_info:
        return SsDecision("REPLAN", "NO_PROGRESS", source="rules",
                          details={"calls": snap.tools.calls})

    # Some measured progress without a fresh evidence flag.
    if snap.progress.objective_progress > 0:
        return SsDecision("NO_OP", "PRODUCTIVE_PROGRESS", source="rules")

    return None


def is_suspicious(snap: Snapshot) -> bool:
    """Interesting-but-not-a-fact → ask the model (or stay conservative)."""
    if snap.tools.errors > 0:
        return True
    if 0 < snap.tools.repeated_calls < REPEAT_N and _no_new_info(snap):
        return True
    if snap.task.elapsed_seconds >= LONG_TASK_SEC and snap.progress.objective_progress < 0.1:
        return True
    if (snap.model.generating and snap.model.streamed
            and SOFT_STALL_SEC <= snap.model.seconds_since_token < STALL_SEC):
        return True
    return False
