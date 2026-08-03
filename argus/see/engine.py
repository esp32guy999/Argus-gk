"""SEE pure supervisor — state machine + stall/loop/verify rules.

No I/O. Persistence is optional via callbacks or argus.see.api.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

from .models import (
    ACTIONS,
    EVENT_TYPES,
    TERMINAL,
    TRANSITIONS,
    Checkpoint,
    Evidence,
    SeeEvent,
    SeeTask,
    SupervisorDecision,
)

# Tunables (env overridable at process level for ops; tests patch these)
IDLE_STALL_SEC = float(os.environ.get("ARGUS_SEE_IDLE_SEC", "120"))
LOOP_REPEAT = int(os.environ.get("ARGUS_SEE_LOOP_REPEAT", "3"))
MAX_EVENTS_KEPT = int(os.environ.get("ARGUS_SEE_MAX_EVENTS", "500"))


class TransitionError(ValueError):
    pass


def _transition(task: SeeTask, to: str, reason: str) -> SeeEvent:
    fr = task.current_state
    if to == fr:
        return SeeEvent(type="StateTransition", detail=f"noop {fr}", data={"reason": reason})
    allowed = TRANSITIONS.get(fr, frozenset())
    if to not in allowed:
        raise TransitionError(f"illegal transition {fr} → {to} ({reason})")
    task.current_state = to
    task.updated_ts = time.time()
    if to == "STALLED":
        task.stall_reason = reason
    elif fr == "STALLED":
        task.stall_reason = None
    ev = SeeEvent(
        type="StateTransition",
        detail=f"{fr}→{to}",
        data={"from": fr, "to": to, "reason": reason},
    )
    return ev


def _append_event(task: SeeTask, ev: SeeEvent) -> None:
    task.event_log.append(ev)
    if len(task.event_log) > MAX_EVENTS_KEPT:
        task.event_log = task.event_log[-MAX_EVENTS_KEPT:]
    task.last_event_ts = ev.ts
    task.updated_ts = time.time()


def create_task(
    goal: str,
    *,
    success_criteria: list[str] | None = None,
    checklist: list[str] | None = None,
    required_evidence: list[str] | None = None,
    constraints: list[str] | None = None,
    importance: int = 50,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> SeeTask:
    """NEW task + TaskCreated event."""
    criteria = list(success_criteria or [])
    task = SeeTask(
        id=task_id or SeeTask.new_id(),
        goal=(goal or "").strip() or "(untitled goal)",
        success_criteria=criteria,
        checklist=list(checklist or criteria),
        required_evidence=list(required_evidence or criteria),
        constraints=list(constraints or []),
        importance=max(0, min(100, int(importance))),
        conversation_id=conversation_id,
        current_state="NEW",
    )
    _append_event(task, SeeEvent(type="TaskCreated", detail=task.goal[:200]))
    return task


def start_planning(task: SeeTask) -> SupervisorDecision:
    ev = _transition(task, "PLANNING", "start_planning")
    _append_event(task, ev)
    _append_event(task, SeeEvent(type="TaskStarted", detail="PLANNING"))
    return SupervisorDecision(action="CONTINUE", reason="planning", new_state="PLANNING")


def accept_plan(
    task: SeeTask,
    *,
    checklist: list[str] | None = None,
    success_criteria: list[str] | None = None,
    required_evidence: list[str] | None = None,
) -> SupervisorDecision:
    if checklist is not None:
        task.checklist = list(checklist)
    if success_criteria is not None:
        task.success_criteria = list(success_criteria)
    if required_evidence is not None:
        task.required_evidence = list(required_evidence)
    if task.current_state == "NEW":
        _append_event(task, _transition(task, "PLANNING", "implicit_plan"))
    _append_event(task, SeeEvent(
        type="PlanGenerated",
        detail=f"{len(task.checklist)} checklist items",
        data={"checklist": task.checklist, "success_criteria": task.success_criteria},
    ))
    ev = _transition(task, "EXECUTING", "plan_accepted")
    _append_event(task, ev)
    task.last_progress_ts = time.time()
    return SupervisorDecision(
        action="CONTINUE",
        reason="plan accepted; worker may execute",
        new_state="EXECUTING",
        feedback="Execute the checklist. Request VERIFY when success criteria have evidence.",
    )


def apply_event(task: SeeTask, event: SeeEvent | dict) -> SupervisorDecision:
    """Ingest one event; update task; may change state (e.g. loop → STALLED)."""
    if task.current_state in TERMINAL:
        return SupervisorDecision(
            action="ABORT", reason="task already terminal", ok=False,
            feedback=f"Task is {task.current_state}; no further events.",
        )
    ev = event if isinstance(event, SeeEvent) else SeeEvent.from_dict(event)
    if ev.type not in EVENT_TYPES and ev.type != "StateTransition":
        # allow unknown but tag
        ev = SeeEvent(type=ev.type, detail=ev.detail, data={**ev.data, "unknown_type": True}, ts=ev.ts)

    _append_event(task, ev)
    now = time.time()

    # --- tool tracking / loop detection ---
    if ev.type == "ToolCalled":
        tool = ev.detail or ev.data.get("tool") or "tool"
        args_key = ev.data.get("args_key") or json.dumps(ev.data.get("args") or {}, sort_keys=True, default=str)
        task.tool_history.append({"tool": tool, "args_key": args_key, "ts": ev.ts, "ok": None})
        # count recent identical
        recent = [h for h in task.tool_history[-20:] if h["tool"] == tool and h["args_key"] == args_key]
        if len(recent) >= LOOP_REPEAT and task.current_state == "EXECUTING":
            _append_event(task, SeeEvent(type="LoopDetected", detail=tool, data={"count": len(recent)}))
            _append_event(task, _transition(task, "STALLED", f"loop:{tool}"))
            task.worker_feedback = (
                f"Supervisor: loop detected on {tool} (×{len(recent)}). "
                f"Change approach, mark a checkpoint, or request REPLAN."
            )
            return SupervisorDecision(
                action="RETRY",
                reason=f"repeated tool {tool}",
                new_state="STALLED",
                feedback=task.worker_feedback,
            )

    if ev.type in ("ToolCompleted", "ToolFailed"):
        ok = ev.type == "ToolCompleted"
        if task.tool_history:
            task.tool_history[-1]["ok"] = ok
        if ok:
            task.last_progress_ts = now

    if ev.type == "CheckpointReached":
        label = ev.detail or ev.data.get("label") or "checkpoint"
        task.checkpoints.append(Checkpoint(label=label, detail=ev.data.get("detail")))
        task.last_progress_ts = now
        # optional: mark checklist item
        item = ev.data.get("checklist_item")
        if item and item in task.checklist and item not in task.completed_items:
            task.completed_items.append(item)
            _append_event(task, SeeEvent(type="ChecklistItemDone", detail=item))

    if ev.type == "ChecklistItemDone":
        item = ev.detail or ""
        if item and item not in task.completed_items:
            task.completed_items.append(item)
            task.last_progress_ts = now

    if ev.type == "EvidenceAdded":
        e = Evidence.from_dict(ev.data if ev.data.get("criterion") else {
            "criterion": ev.detail or "",
            "kind": ev.data.get("kind") or "other",
            "summary": ev.data.get("summary") or ev.detail or "",
            "payload": ev.data.get("payload"),
        })
        task.evidence.append(e)
        task.last_progress_ts = now

    # Auto-resume from STALLED on real progress events
    if task.current_state == "STALLED" and ev.type in (
        "CheckpointReached", "EvidenceAdded", "ChecklistItemDone", "ToolCompleted",
    ):
        _append_event(task, _transition(task, "EXECUTING", f"progress:{ev.type}"))
        return SupervisorDecision(
            action="RESUME",
            reason=f"progress after stall ({ev.type})",
            new_state="EXECUTING",
            feedback="Supervisor: progress observed; resumed EXECUTING.",
        )

    return SupervisorDecision(action="CONTINUE", reason=f"event {ev.type}", new_state=task.current_state)


def tick(task: SeeTask, *, now: float | None = None) -> SupervisorDecision:
    """Periodic stall check — call from a timer or between worker turns."""
    if task.current_state in TERMINAL:
        return SupervisorDecision(action="CONTINUE", reason="terminal", new_state=task.current_state)
    if task.current_state not in ("EXECUTING", "PLANNING", "VERIFYING"):
        return SupervisorDecision(action="CONTINUE", reason=f"state {task.current_state}", new_state=task.current_state)

    now = now if now is not None else time.time()
    idle = now - (task.last_progress_ts or task.last_event_ts or task.created_ts)
    if task.current_state == "EXECUTING" and idle >= IDLE_STALL_SEC:
        _append_event(task, SeeEvent(
            type="StallDetected", detail="idle",
            data={"idle_sec": idle, "threshold": IDLE_STALL_SEC},
        ))
        _append_event(task, _transition(task, "STALLED", f"idle:{idle:.0f}s"))
        task.worker_feedback = (
            f"Supervisor: idle stall ({idle:.0f}s without progress). "
            f"Continue with a new action, add a checkpoint, or ask for REPLAN."
        )
        return SupervisorDecision(
            action="ASK_USER" if idle >= IDLE_STALL_SEC * 3 else "RETRY",
            reason=f"idle {idle:.0f}s",
            new_state="STALLED",
            feedback=task.worker_feedback,
        )
    return SupervisorDecision(action="CONTINUE", reason="healthy", new_state=task.current_state)


_WEAK_EVIDENCE = frozenset({
    "ok", "done", "yes", "yep", "passed", "pass", "success", "fine", "good",
    "works", "working", "complete", "completed", "true", "1", "✓", "✅",
})


def _evidence_quality_issues(task: SeeTask) -> list[str]:
    """Richer VERIFY (S4): reject hollow claims that aren't observable evidence."""
    issues = []
    for e in task.evidence:
        summary = (e.summary or "").strip()
        if len(summary) < 12:
            issues.append(f"evidence too thin for {e.criterion!r}: {summary!r}")
            continue
        if summary.lower() in _WEAK_EVIDENCE:
            issues.append(
                f"evidence for {e.criterion!r} is a bare claim ({summary!r}); "
                f"need observable detail (command output, HTTP status, path, …)"
            )
        if e.kind == "http" and not re.search(
            r"\b([1-5]\d{2}|http|https|curl|status|respond)\b", summary, re.I
        ):
            issues.append(
                f"http evidence for {e.criterion!r} should mention status/URL/response"
            )
        if e.kind == "docker" and not re.search(
            r"\b(docker|container|up|running|compose|health)\b", summary, re.I
        ):
            issues.append(
                f"docker evidence for {e.criterion!r} should mention container/state"
            )
    return issues


def request_verify(task: SeeTask) -> SupervisorDecision:
    """Worker believes it is done — SEE decides COMPLETED vs EXECUTING."""
    if task.current_state in TERMINAL:
        return SupervisorDecision(action="ABORT", reason="terminal", ok=False, new_state=task.current_state)
    if task.current_state == "STALLED":
        _append_event(task, _transition(task, "EXECUTING", "verify_from_stall"))
    if task.current_state not in ("EXECUTING", "VERIFYING"):
        return SupervisorDecision(
            action="RETRY",
            reason=f"cannot verify from {task.current_state}",
            ok=False,
            feedback=f"Supervisor: verify only from EXECUTING (now {task.current_state}).",
            new_state=task.current_state,
        )

    _append_event(task, _transition(task, "VERIFYING", "worker_request"))
    _append_event(task, SeeEvent(type="VerificationStarted", detail="check evidence"))

    missing = task.missing_evidence()
    incomplete = [c for c in task.checklist if c not in task.completed_items]
    weak = _evidence_quality_issues(task)

    if missing or incomplete or weak:
        feedback_parts = []
        if missing:
            feedback_parts.append("missing evidence for: " + "; ".join(missing))
        if incomplete:
            feedback_parts.append("checklist open: " + "; ".join(incomplete))
        if weak:
            feedback_parts.append("weak evidence: " + "; ".join(weak[:4]))
        feedback = "Supervisor: verification failed — " + " | ".join(feedback_parts)
        task.worker_feedback = feedback
        _append_event(task, SeeEvent(
            type="VerificationFailed",
            detail=feedback[:500],
            data={
                "missing_evidence": missing,
                "incomplete_checklist": incomplete,
                "weak_evidence": weak,
            },
        ))
        _append_event(task, _transition(task, "EXECUTING", "verify_failed"))
        return SupervisorDecision(
            action="RETRY",
            reason="evidence incomplete or weak",
            new_state="EXECUTING",
            feedback=feedback,
            ok=False,
        )

    _append_event(task, SeeEvent(type="VerificationPassed", detail="all criteria evidenced"))
    _append_event(task, _transition(task, "COMPLETED", "verified"))
    _append_event(task, SeeEvent(type="TaskCompleted", detail=task.goal[:200]))
    task.worker_feedback = "Supervisor: COMPLETED — evidence satisfied success criteria."
    return SupervisorDecision(
        action="COMPLETE",
        reason="verified",
        new_state="COMPLETED",
        feedback=task.worker_feedback,
    )


def supervisor_action(task: SeeTask, action: str, *, reason: str = "") -> SupervisorDecision:
    """Explicit SEE/user action: RESUME, REPLAN, ABORT, ASK_USER."""
    action = (action or "").upper()
    if action not in ACTIONS:
        return SupervisorDecision(action="CONTINUE", reason="unknown action", ok=False)

    if action == "ABORT":
        if task.current_state not in TERMINAL:
            _append_event(task, _transition(task, "ABORTED", reason or "abort"))
            _append_event(task, SeeEvent(type="TaskAborted", detail=reason or "abort"))
        return SupervisorDecision(action="ABORT", reason=reason or "aborted", new_state="ABORTED")

    if action == "REPLAN":
        if task.current_state in TERMINAL:
            return SupervisorDecision(action="ABORT", reason="terminal", ok=False)
        if task.current_state != "PLANNING":
            # STALLED or EXECUTING → PLANNING
            if task.current_state == "EXECUTING":
                _append_event(task, _transition(task, "STALLED", "replan_prep"))
            _append_event(task, _transition(task, "PLANNING", reason or "replan"))
        _append_event(task, SeeEvent(type="SupervisorAction", detail="REPLAN", data={"reason": reason}))
        task.worker_feedback = "Supervisor: REPLAN — produce a new checklist/plan."
        return SupervisorDecision(
            action="REPLAN", reason=reason or "replan", new_state="PLANNING",
            feedback=task.worker_feedback,
        )

    if action == "RESUME" or action == "CONTINUE":
        if task.current_state == "STALLED":
            _append_event(task, _transition(task, "EXECUTING", reason or "resume"))
            task.worker_feedback = "Supervisor: RESUME execution."
            return SupervisorDecision(
                action="RESUME", reason=reason or "resume", new_state="EXECUTING",
                feedback=task.worker_feedback,
            )
        return SupervisorDecision(action="CONTINUE", reason="already active", new_state=task.current_state)

    if action == "ASK_USER":
        _append_event(task, SeeEvent(type="SupervisorAction", detail="ASK_USER", data={"reason": reason}))
        task.worker_feedback = reason or "Supervisor: need user input."
        if task.current_state == "EXECUTING":
            _append_event(task, _transition(task, "STALLED", "ask_user"))
        return SupervisorDecision(
            action="ASK_USER", reason=reason or "ask_user",
            new_state=task.current_state, feedback=task.worker_feedback,
        )

    if action == "VERIFY":
        return request_verify(task)

    return SupervisorDecision(action=action, reason=reason or action, new_state=task.current_state)


def worker_brief(task: SeeTask) -> str:
    """Compact prompt fragment for the worker (not full chat)."""
    lines = [
        f"[SEE task {task.id} · {task.current_state}]",
        f"Goal: {task.goal}",
        "Success criteria:",
    ]
    for c in task.success_criteria:
        mark = "✓" if c in task.criteria_covered() else "○"
        lines.append(f"  {mark} {c}")
    if task.checklist:
        lines.append("Checklist:")
        for c in task.checklist:
            mark = "✓" if c in task.completed_items else "○"
            lines.append(f"  {mark} {c}")
    if task.worker_feedback:
        lines.append(f"Supervisor: {task.worker_feedback}")
    if task.current_state == "STALLED":
        lines.append(f"Stalled: {task.stall_reason or 'unknown'}")
    lines.append(
        "When criteria have evidence, request verification (do not declare success yourself)."
    )
    return "\n".join(lines)


def maybe_memory_candidates(task: SeeTask) -> list[dict]:
    """Propose cold-ledger candidates from completed SEE evidence (importance later)."""
    if task.current_state != "COMPLETED":
        return []
    out = []
    for e in task.evidence:
        out.append({
            "summary": e.summary or e.criterion,
            "kind": "config" if e.kind in ("docker", "http", "command") else "event",
            "detail": f"see:{task.id} criterion={e.criterion} {e.payload or ''}".strip(),
            "source": f"see:{task.id}",
        })
    if task.goal and task.importance >= 50:
        out.append({
            "summary": f"Completed task: {task.goal[:160]}",
            "kind": "event",
            "detail": f"checklist done: {', '.join(task.completed_items)[:300]}",
            "source": f"see:{task.id}",
        })
    return out[:5]
