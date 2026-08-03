"""SEE public API — persist task objects and run the supervisor.

Greenfield engine + existing Store (SQLite). Memory flywheel is optional sink
on COMPLETED (candidates only).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from . import engine
from .models import SeeEvent, SeeTask, SupervisorDecision

# Active task per conversation (in-process; durable id still in DB)
_CONV_TASK: dict[str, str] = {}
_LOCK = threading.Lock()


def _store():
    from argus.storage import get_store
    return get_store()


def _save(task: SeeTask, *, event: SeeEvent | None = None) -> SeeTask:
    st = _store()
    st.save_see_task(task.to_dict())
    if event is not None:
        st.append_see_event(task.id, event.to_dict())
    # also persist latest event from log if not passed
    elif task.event_log:
        st.append_see_event(task.id, task.event_log[-1].to_dict())
    try:
        from argus import metrics
        metrics.SEE_TASKS.labels(task.current_state).inc()
    except Exception:
        pass
    return task


def create(
    goal: str,
    *,
    success_criteria: list[str] | None = None,
    checklist: list[str] | None = None,
    required_evidence: list[str] | None = None,
    constraints: list[str] | None = None,
    importance: int = 50,
    conversation_id: str | None = None,
    start: bool = True,
) -> SeeTask:
    """Create a SEE task; optionally accept a default plan and enter EXECUTING."""
    task = engine.create_task(
        goal,
        success_criteria=success_criteria,
        checklist=checklist,
        required_evidence=required_evidence,
        constraints=constraints,
        importance=importance,
        conversation_id=conversation_id,
    )
    if start:
        engine.start_planning(task)
        engine.accept_plan(task)  # v1: planner is external; use provided checklist as plan
    _save(task)
    if conversation_id:
        with _LOCK:
            _CONV_TASK[conversation_id] = task.id
    try:
        from argus import metrics
        metrics.SEE_CREATED.inc()
    except Exception:
        pass
    return task


def get(task_id: str) -> SeeTask | None:
    raw = _store().get_see_task(task_id)
    return SeeTask.from_dict(raw) if raw else None


def active_for_conversation(conversation_id: str) -> SeeTask | None:
    with _LOCK:
        tid = _CONV_TASK.get(conversation_id)
    if tid:
        t = get(tid)
        if t and t.current_state not in ("COMPLETED", "ABORTED"):
            return t
    # fallback: newest non-terminal for conv
    for raw in _store().list_see_tasks(conversation_id=conversation_id, include_terminal=False, limit=1):
        return SeeTask.from_dict(raw)
    return None


def bind_conversation(conversation_id: str, task_id: str) -> None:
    with _LOCK:
        _CONV_TASK[conversation_id] = task_id


def on_event(task_id: str, event: SeeEvent | dict) -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return SupervisorDecision(action="ABORT", reason="unknown task", ok=False)
    dec = engine.apply_event(task, event)
    _save(task)
    try:
        from argus import metrics
        metrics.SEE_EVENTS.labels(
            event.type if isinstance(event, SeeEvent) else event.get("type", "?")
        ).inc()
        if dec.new_state == "STALLED":
            metrics.SEE_STALLS.inc()
    except Exception:
        pass
    return dec


def on_tool(
    task_id: str,
    tool: str,
    *,
    args: Any = None,
    ok: bool | None = None,
    phase: str = "call",
) -> SupervisorDecision:
    """Convenience: tool call/complete/fail from the agent loop."""
    import json
    args_key = json.dumps(args or {}, sort_keys=True, default=str)[:500]
    if phase == "call":
        ev = SeeEvent(
            type="ToolCalled", detail=tool,
            data={"tool": tool, "args_key": args_key, "args": args or {}},
        )
    elif ok is False or phase == "fail":
        ev = SeeEvent(type="ToolFailed", detail=tool, data={"tool": tool, "args_key": args_key})
    else:
        ev = SeeEvent(type="ToolCompleted", detail=tool, data={"tool": tool, "args_key": args_key})
    return on_event(task_id, ev)


def checkpoint(task_id: str, label: str, *, checklist_item: str | None = None,
               detail: str | None = None) -> SupervisorDecision:
    return on_event(task_id, SeeEvent(
        type="CheckpointReached", detail=label,
        data={"label": label, "checklist_item": checklist_item, "detail": detail},
    ))


def add_evidence(task_id: str, criterion: str, summary: str, *,
                 kind: str = "other", payload: str | None = None) -> SupervisorDecision:
    return on_event(task_id, SeeEvent(
        type="EvidenceAdded", detail=criterion,
        data={"criterion": criterion, "kind": kind, "summary": summary, "payload": payload},
    ))


def tick(task_id: str) -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return SupervisorDecision(action="ABORT", reason="unknown task", ok=False)
    dec = engine.tick(task)
    _save(task)
    return dec


def request_verify(task_id: str) -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return SupervisorDecision(action="ABORT", reason="unknown task", ok=False)
    dec = engine.request_verify(task)
    _save(task)
    if dec.action == "COMPLETE":
        _emit_memory(task)
        try:
            from argus import metrics
            metrics.SEE_COMPLETED.inc()
        except Exception:
            pass
    return dec


def action(task_id: str, action_name: str, *, reason: str = "") -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return SupervisorDecision(action="ABORT", reason="unknown task", ok=False)
    dec = engine.supervisor_action(task, action_name, reason=reason)
    _save(task)
    return dec


def worker_brief(task_id: str) -> str:
    task = get(task_id)
    if not task:
        return ""
    return engine.worker_brief(task)


def _emit_memory(task: SeeTask) -> None:
    """Push high-level candidates into memory_policy (cold only)."""
    if os.environ.get("ARGUS_SEE_MEMORY", "1") in ("0", "false", "no"):
        return
    try:
        from argus import memory_policy as mp
        for cand in engine.maybe_memory_candidates(task):
            mp.accept_candidate(
                cand["summary"],
                kind=cand.get("kind") or "event",
                detail=cand.get("detail"),
                source=cand.get("source") or f"see:{task.id}",
                conversation_id=task.conversation_id,
                policy="see",
            )
    except Exception:
        pass
