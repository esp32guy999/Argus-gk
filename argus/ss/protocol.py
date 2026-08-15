"""SUPER-SEE (SS) control protocol — v1.

SS-native vocabulary is the small JSON the rules/model emit.
STP mapping is advisory only: v1 is shadow mode and never applies these
to a SeeTask. Live authority is a later phase.

Do not invent a second supervisor. SEE owns transitions (specs/see-protocol.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# SS-native actions (v1). CONTINUE/CHANGE_APPROACH/NEW_GOAL from the vision
# doc collapse onto these so we do not fork SEE's vocabulary.
ACTIONS = (
    "NO_OP",
    "WAIT",
    "RETRY",
    "REPLAN",
    "VERIFY",
    "ABORT",
    "ASK_USER",
)
ACTIONS_SET = frozenset(ACTIONS)

# Vision-doc synonyms → canonical SS action.
ACTION_SYNONYMS = {
    "CONTINUE": "NO_OP",
    "CHANGE_APPROACH": "REPLAN",
    "NEW_GOAL": "REPLAN",
    "NOOP": "NO_OP",
    "NONE": "NO_OP",
    "IDLE": "NO_OP",
}

REASON_CODES = (
    "PRODUCTIVE_PROGRESS",
    "NORMAL_WAIT",
    "INFERENCE_STALL",
    "TOOL_STALL",
    "REPEATED_TOOL_CALL",
    "NO_PROGRESS",
    "LOOP_DETECTED",
    "OBJECTIVE_COMPLETE",
    "OBJECTIVE_BLOCKED",
    "INVALID_STATE",
    "RESOURCE_FAILURE",
    "UNKNOWN",
)
REASONS_SET = frozenset(REASON_CODES)

# SS action → (STP action, default STP code). Applied only if authority is on.
SS_TO_STP: dict[str, tuple[str, str]] = {
    "NO_OP":    ("NO_OP",    "NO_OP"),
    "WAIT":     ("STALL",    "TIMEOUT"),
    "RETRY":    ("RETRY",    "TEMPORARY_FAILURE"),
    "REPLAN":   ("REPLAN",   "NO_PROGRESS"),
    "VERIFY":   ("CONTINUE", "WORKER_ACTIVE"),
    "ABORT":    ("ABORT",    "UNRECOVERABLE_ERROR"),
    "ASK_USER": ("ASK_USER", "MISSING_INFORMATION"),
}

REASON_TO_STP: dict[str, str] = {
    "PRODUCTIVE_PROGRESS": "PROGRESS_DETECTED",
    "NORMAL_WAIT":         "WORKER_ACTIVE",
    "INFERENCE_STALL":     "TIMEOUT",
    "TOOL_STALL":          "TIMEOUT",
    "REPEATED_TOOL_CALL":  "LOOP_DETECTED",
    "NO_PROGRESS":         "NO_PROGRESS",
    "LOOP_DETECTED":       "LOOP_DETECTED",
    "OBJECTIVE_COMPLETE":  "ALL_CRITERIA_MET",
    "OBJECTIVE_BLOCKED":   "PRECONDITION_FAILED",
    "INVALID_STATE":       "ILLEGAL_STATE",
    "RESOURCE_FAILURE":    "RESOURCE_MISSING",
    "UNKNOWN":             "EVENT_APPLIED",
}


def normalize_action(raw: str | None) -> str | None:
    if not raw:
        return None
    a = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    a = ACTION_SYNONYMS.get(a, a)
    return a if a in ACTIONS_SET else None


def normalize_reason(raw: str | None) -> str | None:
    if not raw:
        return None
    r = str(raw).strip().upper().replace("-", "_").replace(" ", "_")
    return r if r in REASONS_SET else None


def stp_for(action: str, reason: str) -> tuple[str, str]:
    stp_action, default_code = SS_TO_STP.get(action, ("NO_OP", "EVENT_APPLIED"))
    return stp_action, REASON_TO_STP.get(reason, default_code)


@dataclass
class SsDecision:
    """SS output. authority is always False in v1."""
    action: str
    reason_code: str
    source: str = "rules"          # rules | model | fallback
    authority: bool = False
    stp_action: str = ""
    stp_code: str = ""
    latency_ms: float | None = None
    raw: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        act = normalize_action(self.action)
        if act is None:
            raise ValueError(f"invalid SS action {self.action!r}")
        rea = normalize_reason(self.reason_code)
        if rea is None:
            raise ValueError(f"invalid SS reason_code {self.reason_code!r}")
        self.action = act
        self.reason_code = rea
        if not self.stp_action or not self.stp_code:
            self.stp_action, self.stp_code = stp_for(act, rea)
        # v1 hard rule — never take the wheel
        self.authority = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "reason_code": self.reason_code,
            "source": self.source,
            "authority": False,
            "stp_action": self.stp_action,
            "stp_code": self.stp_code,
        }
        if self.latency_ms is not None:
            d["latency_ms"] = round(self.latency_ms, 1)
        if self.raw:
            d["raw"] = self.raw[:240]
        if self.details:
            d["details"] = dict(self.details)
        return d
