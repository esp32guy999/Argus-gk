"""SEE types — task object, states, events, supervisor actions.

Canonical work unit for the Supervisory Execution Engine (specs/see.md).
JSON-serializable plain data; no I/O.
"""
from __future__ import annotations

import re
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

# ── Supervisory Task Protocol (STP) ──────────────────────────────────────
# Versioned network protocol (not merely Python classes). Changing ACTIONS or
# CODES is a protocol revision: update specs/see-protocol.md + tests + workers.
#
# STP-1.1: adds NO_OP action + worker step response contract (every /task step
# yields exactly one decision object — never empty/null/EOF).
PROTOCOL_ID = "STP-1.1"
PROTOCOL_MAJOR = 1  # workers reject major != 1
# Accepted protocol ids for this major (emitters use PROTOCOL_ID).
PROTOCOL_ACCEPTED = frozenset({"STP-1.0", "STP-1.1"})

# Action vocabulary (STP-1.1). NO_OP = intentional no work this step.
ACTIONS = (
    "CONTINUE",
    "RETRY",
    "REPLAN",
    "VERIFY_FAILED",
    "ASK_USER",
    "STALL",
    "ABORT",
    "COMPLETE",
    "NO_OP",
)
ACTIONS_SET = frozenset(ACTIONS)

# Human/CLI layer only — never emitted as action by the Supervisor.
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
    "RESUME": "CONTINUE",
    "VERIFY": "CONTINUE",
    # Intentional idle / nothing-to-do
    "NOOP": "NO_OP",
    "NONE": "NO_OP",
    "IDLE": "NO_OP",
    "NOTHING": "NO_OP",
    "SKIP": "NO_OP",
}

# Frozen code registry (STP § Recommendation 3). Unknown codes rejected.
CODES = (
    # Progress / continue
    "PROGRESS_DETECTED",
    "CHECKPOINT_ACCEPTED",
    "WORKER_ACTIVE",
    "PLAN_ACCEPTED",
    "EVENT_APPLIED",
    "RESUMED",
    "NO_OP",                 # intentional no work (step evaluated; nothing to do)
    "STEP_ACCEPTED",         # worker step contract accepted
    "MALFORMED_STEP",        # worker step failed response contract
    # Verification
    "MISSING_EVIDENCE",
    "WEAK_EVIDENCE",
    "UNRELATED_EVIDENCE",
    "CRITERION_NOT_MET",
    "VERIFICATION_FAILED",
    "ALL_CRITERIA_MET",
    "VALIDATION_FAILED",
    # Planning
    "PLAN_INVALID",
    "RESOURCE_MISSING",
    "PRECONDITION_FAILED",
    "NEW_INFORMATION",
    # Execution
    "COMMAND_FAILED",
    "TEMPORARY_FAILURE",
    "TIMEOUT",
    "LOOP_DETECTED",
    "NO_PROGRESS",
    "WAITING_FOREVER",
    # User
    "MISSING_INFORMATION",
    "AMBIGUOUS_REQUEST",
    "PERMISSION_REQUIRED",
    "CONFIGURATION_UNKNOWN",
    # Terminal / control
    "UNRECOVERABLE_ERROR",
    "MAX_RETRIES",
    "SECURITY_POLICY",
    "USER_CANCELLED",
    "UNKNOWN_TASK",
    "ILLEGAL_STATE",
    "PROTOCOL_ERROR",
)
CODES_SET = frozenset(CODES)

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
    "WorkerStepReported",
    "TaskCompleted",
    "TaskAborted",
    "StateTransition",
)


@dataclass
class Evidence:
    """Observable proof for a success criterion (not a model claim).

    Provenance (STP rec 6): tool-produced evidence should rank above worker text.
    """
    criterion: str           # which success_criteria item this supports
    kind: str                # http|command|file|test|docker|other
    summary: str             # short human line
    payload: str | None = None  # raw snippet / url / path (capped by store)
    ts: float = field(default_factory=time.time)
    source: str | None = None     # e.g. tool:curl, worker:claim, see:path
    command: str | None = None    # originating command if any
    trust: float = 0.5            # 0..1 telemetry; tool evidence default higher

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        src = d.get("source")
        trust = d.get("trust")
        if trust is None:
            trust = 0.9 if (src or "").startswith("tool:") else 0.5
        return cls(
            criterion=d.get("criterion") or "",
            kind=d.get("kind") or "other",
            summary=d.get("summary") or "",
            payload=d.get("payload"),
            ts=float(d.get("ts") or time.time()),
            source=src,
            command=d.get("command"),
            trust=float(trust),
        )

    @property
    def is_tool_sourced(self) -> bool:
        return (self.source or "").startswith("tool:")


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


def _parse_protocol(value: str | None) -> str:
    """Return canonical protocol id or raise PROTOCOL_ERROR.

    Accepts STP-1.0 and STP-1.1 (same major). Emitters always write PROTOCOL_ID.
    """
    if not value:
        return PROTOCOL_ID  # emitters may omit; workers should require it
    v = str(value).strip().upper().replace("_", "-")
    # normalize STP1.1 → STP-1.1
    m_loose = re.match(r"STP-?(\d+)(?:\.(\d+))?", v)
    if m_loose:
        major = int(m_loose.group(1))
        if major != PROTOCOL_MAJOR:
            raise ValueError(
                f"incompatible STP protocol {value!r}; this runtime speaks {PROTOCOL_ID}"
            )
        minor = m_loose.group(2) or "0"
        candidate = f"STP-{major}.{minor}"
        if candidate in PROTOCOL_ACCEPTED or candidate == PROTOCOL_ID:
            return PROTOCOL_ID  # normalize to current emitter id
        # accept any 1.x we don't know yet? fail-closed on unknown minor for safety
        if major == 1 and int(minor) <= 1:
            return PROTOCOL_ID
        raise ValueError(f"unknown STP protocol {value!r}; expected {PROTOCOL_ID}")
    raise ValueError(f"unknown STP protocol {value!r}; expected {PROTOCOL_ID}")


@dataclass
class SupervisorDecision:
    """STP decision object — structured, versioned, machine-readable.

    Public schema:
      protocol, action, code, state, blocking, details, confidence?

    confidence is telemetry only and MUST NOT affect execution (STP rec 7).
    """
    action: str
    code: str
    state: str
    blocking: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    confidence: float | None = None
    protocol: str = PROTOCOL_ID

    def __post_init__(self) -> None:
        self.protocol = _parse_protocol(self.protocol)
        act = (self.action or "").upper().strip()
        # Supervisor emitters must use canonical actions; synonyms only at human edge.
        if act not in ACTIONS_SET:
            # allow synonym only when constructing via from_dict(normalize=True)
            if act in ACTION_SYNONYMS:
                act = ACTION_SYNONYMS[act]
            else:
                raise ValueError(
                    f"invalid Supervisor action {self.action!r}; "
                    f"allowed: {', '.join(ACTIONS)}"
                )
        self.action = act
        code = (self.code or "EVENT_APPLIED").upper().strip()
        if code not in CODES_SET:
            raise ValueError(
                f"invalid Supervisor code {self.code!r}; "
                f"not in STP code registry"
            )
        self.code = code
        self.state = (self.state or "").upper() or "EXECUTING"
        if self.state not in STATES and self.state not in TERMINAL:
            # allow only known states
            if self.state not in STATES:
                raise ValueError(f"invalid state {self.state!r}")
        if self.blocking is None:
            self.blocking = []
        if self.details is None:
            self.details = {}
        # confidence must not be used for control — clamp if present
        if self.confidence is not None:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))

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
        return self.action not in ("ABORT", "VERIFY_FAILED")

    def to_dict(self) -> dict:
        """Public STP payload. Workers must depend only on these fields."""
        d = {
            "protocol": self.protocol,
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
    def from_dict(cls, d: dict, *, allow_synonyms: bool = True) -> "SupervisorDecision":
        """Parse a decision. Rejects unknown protocol, action, code, or state.

        allow_synonyms: human/CLI edge may pass TRY_AGAIN → RETRY; Supervisor
        emitters should pass allow_synonyms=False.
        """
        if not isinstance(d, dict):
            raise ValueError("decision must be an object")
        # Required fields (STP fail-fast)
        for req in ("action", "code", "state"):
            if req not in d or d[req] in (None, ""):
                raise ValueError(f"malformed STP decision: missing required field {req!r}")
        proto = _parse_protocol(d.get("protocol"))
        action = str(d.get("action") or "").upper().strip()
        if allow_synonyms and action in ACTION_SYNONYMS:
            action = ACTION_SYNONYMS[action]
        if action not in ACTIONS_SET:
            raise ValueError(f"unknown Supervisor action {d.get('action')!r} — rejected")
        code = str(d.get("code") or "").upper().strip()
        if code not in CODES_SET:
            raise ValueError(f"unknown Supervisor code {d.get('code')!r} — rejected")
        state = str(d.get("state") or "").upper().strip()
        if state not in STATES:
            raise ValueError(f"unknown state {d.get('state')!r} — rejected")
        return cls(
            protocol=proto,
            action=action,
            code=code,
            state=state,
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
    """Factory for STP decisions (always PROTOCOL_ID)."""
    return SupervisorDecision(
        protocol=PROTOCOL_ID,
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
