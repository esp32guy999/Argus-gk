"""SEE /task response contract — every worker step yields one decision object.

Inspired by OpenClaw's "always respond", but as a *protocol* guarantee for
supervised tasks (not a chat UI delivery token).

Invariant:
  Every see_report_step / worker step submission is exactly one valid object.
  Empty, null, EOF, malformed JSON, or missing action → rejected (MALFORMED_STEP).

NO_OP means the step was evaluated successfully and intentional idle is correct:
  no tool, no user reply, no retry required.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import (
    ACTION_SYNONYMS,
    ACTIONS_SET,
    CODES_SET,
    PROTOCOL_ID,
    _parse_protocol,
)

# Outcomes a *worker* may declare on a step (subset of STP + report-only).
# COMPLETE still routes through supervisor VERIFY — worker cannot self-complete.
WORKER_STEP_ACTIONS = frozenset({
    "CONTINUE",
    "RETRY",
    "REPLAN",
    "ASK_USER",
    "STALL",
    "ABORT",
    "COMPLETE",      # means "request verify" — never auto-COMPLETED
    "NO_OP",
})

# Default codes when worker omits code
_DEFAULT_CODE = {
    "CONTINUE": "WORKER_ACTIVE",
    "RETRY": "TEMPORARY_FAILURE",
    "REPLAN": "NEW_INFORMATION",
    "ASK_USER": "MISSING_INFORMATION",
    "STALL": "NO_PROGRESS",
    "ABORT": "UNRECOVERABLE_ERROR",
    "COMPLETE": "ALL_CRITERIA_MET",  # still verified by supervisor
    "NO_OP": "NO_OP",
}


class ContractError(ValueError):
    """Response contract violation — empty, malformed, or unknown vocabulary."""

    def __init__(self, message: str, *, code: str = "MALFORMED_STEP"):
        super().__init__(message)
        self.code = code


@dataclass
class WorkerStepResult:
    """One deterministic decision from a worker step (response contract object).

    NO_OP means: state was evaluated and no mutation was required — not "nothing
    happened" and not task progress.
    """
    action: str
    code: str
    message: str = ""              # human-readable explanation (preferred)
    reason: str = ""               # alias of message (legacy)
    explanation: str = ""          # alias of message
    confidence: float | None = None
    evidence: list[dict] = field(default_factory=list)
    tool: str | None = None        # optional tool name if EXECUTE-style
    details: dict = field(default_factory=dict)
    protocol: str = PROTOCOL_ID
    state: str | None = None       # optional; supervisor fills current task state

    def __post_init__(self) -> None:
        self.protocol = _parse_protocol(self.protocol)
        act = (self.action or "").upper().strip()
        if act in ACTION_SYNONYMS:
            act = ACTION_SYNONYMS[act]
        if act not in WORKER_STEP_ACTIONS:
            raise ContractError(
                f"worker step action {self.action!r} not allowed; "
                f"use one of {sorted(WORKER_STEP_ACTIONS)}"
            )
        self.action = act
        code = (self.code or _DEFAULT_CODE.get(act, "STEP_ACCEPTED")).upper().strip()
        if code not in CODES_SET:
            raise ContractError(f"unknown code {self.code!r} — rejected")
        self.code = code
        # Prefer message; fold legacy aliases
        text = (self.message or self.reason or self.explanation or "").strip()
        self.message = text
        self.reason = text  # keep reason populated for older callers
        if self.confidence is not None:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.evidence is None:
            self.evidence = []
        if self.details is None:
            self.details = {}

    @property
    def is_no_op(self) -> bool:
        return self.action == "NO_OP"

    def message_text(self) -> str:
        return self.message or self.reason or self.explanation or ""

    def to_dict(self) -> dict:
        d = {
            "protocol": self.protocol,
            "action": self.action,
            "code": self.code,
            "message": self.message_text(),
            "evidence": list(self.evidence),
            "details": dict(self.details),
        }
        # Legacy mirror for older consumers
        if self.message_text():
            d["reason"] = self.message_text()
        if self.confidence is not None:
            d["confidence"] = self.confidence
        if self.tool:
            d["tool"] = self.tool
        if self.state:
            d["state"] = self.state
        return d


def _coerce_mapping(raw: Any) -> dict:
    """Normalize raw step input to a dict or raise ContractError."""
    if raw is None:
        raise ContractError(
            "empty response: worker step is null/None — every /task step must "
            "return a decision object (use action=NO_OP if nothing to do)"
        )
    if isinstance(raw, WorkerStepResult):
        return raw.to_dict()
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ContractError(
                "empty response: worker step string is blank — emit a decision "
                "object or action=NO_OP"
            )
        try:
            raw = json.loads(s)
        except json.JSONDecodeError as e:
            raise ContractError(
                f"malformed JSON worker step: {e}"
            ) from e
    if not isinstance(raw, dict):
        raise ContractError(
            f"worker step must be an object, got {type(raw).__name__}"
        )
    if not raw:
        raise ContractError("empty response: worker step object has no fields")
    return raw


def parse_worker_step(raw: Any, *, require_protocol: bool = False) -> WorkerStepResult:
    """Parse and validate a worker step decision. Never returns a partial object.

    Failures always raise ContractError (harness treats as protocol FAIL, not success).
    """
    d = _coerce_mapping(raw)

    # Accept alternate field names from the design note
    action = d.get("action") or d.get("decision") or d.get("status")
    if action is None or str(action).strip() == "":
        raise ContractError(
            "malformed step: missing required field 'action' "
            "(or decision/status) — empty action is not allowed"
        )

    # COMPLETE/NO_OP style status from design examples
    act_u = str(action).upper().strip()
    if act_u in ("COMPLETE", "COMPLETED") and d.get("action") is None and d.get("decision") is None:
        # {"status": "COMPLETE", "action": "NO_OP"} form
        if d.get("action"):
            pass
        elif str(d.get("action") or d.get("status") or "").upper() in ("COMPLETE", "COMPLETED"):
            # if only status=COMPLETE without nested action, treat as COMPLETE request
            action = "COMPLETE"

    # Nested: status COMPLETE + action NO_OP
    status = str(d.get("status") or "").upper().strip()
    nested_action = d.get("action")
    if status in ("COMPLETE", "COMPLETED") and nested_action:
        na = str(nested_action).upper().strip()
        if na in ACTION_SYNONYMS:
            na = ACTION_SYNONYMS[na]
        if na == "NO_OP":
            # Explicit no-op completion of *this step*, not task COMPLETE
            action = "NO_OP"
        elif d.get("decision"):
            action = d.get("decision")
        else:
            action = nested_action

    if require_protocol and not d.get("protocol"):
        raise ContractError("malformed step: missing required field 'protocol'")

    code = d.get("code")
    if not code and action:
        au = str(action).upper().strip()
        if au in ACTION_SYNONYMS:
            au = ACTION_SYNONYMS[au]
        code = _DEFAULT_CODE.get(au, "STEP_ACCEPTED")

    message = (
        d.get("message") or d.get("reason") or d.get("explanation") or ""
    )
    evidence = d.get("evidence")
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise ContractError("malformed step: 'evidence' must be a list")

    try:
        return WorkerStepResult(
            protocol=d.get("protocol") or PROTOCOL_ID,
            action=str(action),
            code=str(code or "STEP_ACCEPTED"),
            message=str(message or ""),
            reason=str(message or ""),
            explanation=str(d.get("explanation") or ""),
            confidence=d.get("confidence"),
            evidence=list(evidence),
            tool=(str(d["tool"]) if d.get("tool") else None),
            details=dict(d.get("details") or {}),
            state=(str(d["state"]) if d.get("state") else None),
        )
    except ContractError:
        raise
    except ValueError as e:
        raise ContractError(str(e)) from e


def assert_step_result(raw: Any) -> WorkerStepResult:
    """Harness helper: assert result.action is not None (contract invariant)."""
    step = parse_worker_step(raw)
    if not step.action:
        raise ContractError("invariant violated: action is empty after parse")
    return step


def no_op_step(
    message: str = "State was evaluated and no mutation was required.",
    *,
    confidence: float = 1.0,
    reason: str | None = None,
) -> dict:
    """Canonical NO_OP: evaluated, no mutation required (not empty, not progress)."""
    text = reason if reason is not None else message
    return WorkerStepResult(
        action="NO_OP",
        code="NO_OP",
        message=text,
        confidence=confidence,
        evidence=[],
    ).to_dict()
