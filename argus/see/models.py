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

# Supervisor actions (what the engine recommends / enacts)
ACTIONS = (
    "CONTINUE",   # healthy
    "RETRY",      # repeat last step
    "REPLAN",     # back to PLANNING
    "ASK_USER",   # need human
    "ABORT",      # give up
    "VERIFY",     # enter verification
    "COMPLETE",   # verification passed
    "RESUME",     # STALLED → EXECUTING
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
    """Result of a supervisor evaluation."""
    action: str                    # one of ACTIONS
    reason: str
    new_state: str | None = None   # if a transition was applied
    feedback: str | None = None    # message for the worker
    ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


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
