"""SEE types — task object, states, events, supervisor actions.

Canonical work unit for the Supervisory Execution Engine (specs/see.md).
JSON-serializable plain data; no I/O.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


# ── States (SEE owns all transitions) ────────────────────────────────────
STATES = (
    "NEW",
    "PLANNING",
    "EXECUTING",
    "VERIFYING",
    "COMPLETED",
    "STALLED",
    "ABORTED",
)
TERMINAL = frozenset({"COMPLETED", "ABORTED"})

# Legal transitions: from → allowed to
TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW":       frozenset({"PLANNING", "ABORTED"}),
    "PLANNING":  frozenset({"EXECUTING", "ABORTED"}),
    "EXECUTING": frozenset({"VERIFYING", "STALLED", "ABORTED", "EXECUTING"}),  # EXECUTING: checkpoint
    "VERIFYING": frozenset({"COMPLETED", "EXECUTING", "ABORTED"}),
    "STALLED":   frozenset({"EXECUTING", "PLANNING", "ABORTED"}),  # continue / replan / abort
    "COMPLETED": frozenset(),
    "ABORTED":   frozenset(),
}

# ── Supervisor Decision Protocol v1 (immutable within protocol version) ──
# The Worker MUST accept only these values. Synonyms are NOT allowed.
ACTIONS = (
    "CONTINUE",       # healthy progress
    "RETRY",          # repeat current step; plan still valid
    "REPLAN",         # plan invalid → PLANNING
    "VERIFY_FAILED",  # verification denied → EXECUTING
    "ASK_USER",       # need human → STALLED
    "STALL",          # no progress / loop → STALLED
    "ABORT",          # unrecoverable → ABORTED
    "COMPLETE",       # verified success → COMPLETED
)
ACTIONS_SET = frozenset(ACTIONS)

# Legacy / informal synonyms → canonical action (workers/orchestrators may map)
ACTION_SYNONYMS = {
    "WAIT": "ASK_USER",
    "WAITING": "ASK_USER",
    "SUCCESS": "COMPLETE",
    "FINISHED": "COMPLETE",
    "DONE": "COMPLETE",
    "TRY_AGAIN": "RETRY",
    "ERROR": "RETRY",
    "FAIL": "VERIFY_FAILED",
    "FAILED": "VERIFY_FAILED",
    "STOP": "ABORT",
    "RESUME": "CONTINUE",   # resume is CONTINUE after STALLED→EXECUTING
    "VERIFY": "CONTINUE",   # worker requests verify; supervisor answers COMPLETE/VERIFY_FAILED
}

# Common reason codes (not exhaustive; free-form codes allowed if action is valid)
CODES = (
    "PROGRESS_DETECTED",
    "CHECKPOINT_ACCEPTED",
    "WORKER_ACTIVE",
    "MISSING_EVIDENCE",
    "UNRELATED_EVIDENCE",
    "WEAK_EVIDENCE",
    "COMMAND_FAILED",
    "TEMPORARY_FAILURE",
    "VALIDATION_FAILED",
    "PLAN_INVALID",
    "RESOURCE_MISSING",
    "PRECONDITION_FAILED",
    "NEW_INFORMATION",
    "CRITERION_NOT_MET",
    "VERIFICATION_FAILED",
    "MISSING_INFORMATION",
    "AMBIGUOUS_REQUEST",
    "PERMISSION_REQUIRED",
    "CONFIGURATION_UNKNOWN",
    "NO_PROGRESS",
    "TIMEOUT",
    "LOOP_DETECTED",
    "WAITING_FOREVER",
    "UNRECOVERABLE_ERROR",
    "MAX_RETRIES",
    "SECURITY_POLICY",
    "USER_CANCELLED",
    "ALL_CRITERIA_MET",
    "UNKNOWN_TASK",
    "ILLEGAL_STATE",
    "RESUMED",
    "PLAN_ACCEPTED",
    "EVENT_APPLIED",
)

# Event types (worker/tools/supervisor emit these)
EVENT_TYPES = (
    "TaskCreated",
    "TaskStarted",
    "PlanGenerated",
    "ToolCalled",
    "ToolCompleted",
    "ToolFailed",
    "CheckpointReached",
    "EvidenceAdded",
    "ChecklistItemDone",
    "VerificationStarted",
    "VerificationPassed",
    "VerificationFailed",
    "StallDetected",
    "LoopDetected",
    "SupervisorAction",
    "TaskCompleted",
    "TaskAborted",
    "StateTransition",
)


@dataclass
class Evidence:
    """Observable proof for a success criterion (not a model claim)."""
    criterion: str           # which success_criteria item this supports
    kind: str                # http|command|file|test|docker|other
    summary: str             # short human line
    payload: str | None = None  # raw snippet / url / path (capped by store)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        return cls(
            criterion=d.get("criterion") or "",
            kind=d.get("kind") or "other",
            summary=d.get("summary") or "",
            payload=d.get("payload"),
            ts=float(d.get("ts") or time.time()),
        )


@dataclass
class Checkpoint:
    label: str
    detail: str | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(label=d.get("label") or "", detail=d.get("detail"),
                   ts=float(d.get("ts") or time.time()))


@dataclass
class SeeEvent:
    type: str
    detail: str | None = None
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"type": self.type, "detail": self.detail, "data": self.data, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict) -> "SeeEvent":
        return cls(
            type=d.get("type") or "ToolCalled",
            detail=d.get("detail"),
            data=dict(d.get("data") or {}),
            ts=float(d.get("ts") or time.time()),
        )


@dataclass
class SupervisorDecision:
    """Supervisor Decision Protocol v1 — structured decision for the Worker.

    Schema:
      action, code, state, blocking, details, confidence?
    Legacy aliases kept for older call sites: reason→code, new_state→state,
    feedback→blocking joined, ok→action not ABORT/VERIFY_FAILED.
    """
    action: str
    code: str
    state: str
    blocking: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    confidence: float | None = None

    def __post_init__(self) -> None:
        act = (self.action or "").upper().strip()
        if act in ACTION_SYNONYMS:
            act = ACTION_SYNONYMS[act]
        if act not in ACTIONS_SET:
            raise ValueError(
                f"invalid Supervisor action {self.action!r}; "
                f"allowed: {', '.join(ACTIONS)}"
            )
        self.action = act
        self.code = (self.code or "EVENT_APPLIED").upper()
        self.state = (self.state or "").upper() or "EXECUTING"
        if self.blocking is None:
            self.blocking = []
        if self.details is None:
            self.details = {}

    # ── legacy property aliases (tests / older UI) ──────────────────────
    @property
    def reason(self) -> str:
        return self.code

    @property
    def new_state(self) -> str:
        return self.state

    @property
    def feedback(self) -> str:
        if self.blocking:
            return "Supervisor: " + " | ".join(self.blocking)
        return f"Supervisor: {self.action} ({self.code})"

    @property
    def ok(self) -> bool:
        # Terminal fail / verify deny are not "ok" for the worker happy path.
        return self.action not in ("ABORT", "VERIFY_FAILED")

    def to_dict(self) -> dict:
        """Public protocol payload — workers must only depend on these fields."""
        d = {
            "action": self.action,
            "code": self.code,
            "state": self.state,
            "blocking": list(self.blocking),
            "details": dict(self.details),
        }
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SupervisorDecision":
        """Parse a decision; reject unknown actions (Worker requirement)."""
        if not isinstance(d, dict):
            raise ValueError("decision must be an object")
        action = (d.get("action") or "").upper().strip()
        if action in ACTION_SYNONYMS:
            action = ACTION_SYNONYMS[action]
        if action not in ACTIONS_SET:
            raise ValueError(f"unknown Supervisor action {d.get('action')!r} — rejected")
        return cls(
            action=action,
            code=str(d.get("code") or "EVENT_APPLIED"),
            state=str(d.get("state") or "EXECUTING"),
            blocking=list(d.get("blocking") or []),
            details=dict(d.get("details") or {}),
            confidence=d.get("confidence"),
        )


def decide(
    action: str,
    code: str,
    state: str,
    *,
    blocking: list[str] | None = None,
    details: dict | None = None,
    confidence: float | None = None,
) -> SupervisorDecision:
    """Factory for protocol decisions."""
    return SupervisorDecision(
        action=action,
        code=code,
        state=state,
        blocking=list(blocking or []),
        details=dict(details or {}),
        confidence=confidence,
    )


@dataclass
class SeeTask:
    """Canonical work unit — supervisor reads this, not the full chat."""
    id: str
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    completed_items: list[str] = field(default_factory=list)
    current_state: str = "NEW"
    importance: int = 50
    constraints: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)  # criterion keys needing proof
    evidence: list[Evidence] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    tool_history: list[dict] = field(default_factory=list)  # [{tool, args_key, ts, ok}]
    event_log: list[SeeEvent] = field(default_factory=list)
    conversation_id: str | None = None
    job_id: str | None = None
    last_event_ts: float = field(default_factory=time.time)
    last_progress_ts: float = field(default_factory=time.time)
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    stall_reason: str | None = None
    worker_feedback: str | None = None  # last supervisor message to worker

    @staticmethod
    def new_id() -> str:
        return "see-" + uuid.uuid4().hex[:12]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "checklist": list(self.checklist),
            "completed_items": list(self.completed_items),
            "current_state": self.current_state,
            "importance": self.importance,
            "constraints": list(self.constraints),
            "required_evidence": list(self.required_evidence),
            "evidence": [e.to_dict() for e in self.evidence],
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "tool_history": list(self.tool_history),
            "event_log": [e.to_dict() for e in self.event_log[-200:]],  # cap in dict export
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "last_event_ts": self.last_event_ts,
            "last_progress_ts": self.last_progress_ts,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "stall_reason": self.stall_reason,
            "worker_feedback": self.worker_feedback,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SeeTask":
        return cls(
            id=d["id"],
            goal=d.get("goal") or "",
            success_criteria=list(d.get("success_criteria") or []),
            checklist=list(d.get("checklist") or []),
            completed_items=list(d.get("completed_items") or []),
            current_state=d.get("current_state") or "NEW",
            importance=int(d.get("importance") or 50),
            constraints=list(d.get("constraints") or []),
            required_evidence=list(d.get("required_evidence") or []),
            evidence=[Evidence.from_dict(x) for x in (d.get("evidence") or [])],
            checkpoints=[Checkpoint.from_dict(x) for x in (d.get("checkpoints") or [])],
            tool_history=list(d.get("tool_history") or []),
            event_log=[SeeEvent.from_dict(x) for x in (d.get("event_log") or [])],
            conversation_id=d.get("conversation_id"),
            job_id=d.get("job_id"),
            last_event_ts=float(d.get("last_event_ts") or time.time()),
            last_progress_ts=float(d.get("last_progress_ts") or time.time()),
            created_ts=float(d.get("created_ts") or time.time()),
            updated_ts=float(d.get("updated_ts") or time.time()),
            stall_reason=d.get("stall_reason"),
            worker_feedback=d.get("worker_feedback"),
        )

    def criteria_covered(self) -> set[str]:
        return {e.criterion for e in self.evidence if e.criterion}

    def missing_evidence(self) -> list[str]:
        need = self.required_evidence or list(self.success_criteria)
        covered = self.criteria_covered()
        return [c for c in need if c not in covered]
