"""SEE public API — persist task objects and run the supervisor.

Greenfield engine + existing Store (SQLite). Memory flywheel is optional sink
on COMPLETED (candidates only).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from . import engine
from .models import SeeEvent, SeeTask, SupervisorDecision, decide

# Active task per conversation (in-process; durable id still in DB)
_CONV_TASK: dict[str, str] = {}
_LOCK = threading.Lock()


def _store():
    from argus.storage import get_store
    return get_store()


def _save(task: SeeTask, *, since_n: int | None = None) -> SeeTask:
    """Persist task JSON and flush new events for replay (success criterion #7).

    Pass since_n=len(event_log) *before* a multi-event engine call so every new
    event is written to see_events (not only the last).
    """
    st = _store()
    st.save_see_task(task.to_dict())
    if since_n is not None:
        for ev in task.event_log[since_n:]:
            st.append_see_event(task.id, ev.to_dict())
    elif task.event_log:
        st.append_see_event(task.id, task.event_log[-1].to_dict())
    try:
        from argus import metrics
        metrics.SEE_TASKS.labels(task.current_state).inc()
    except Exception:
        pass
    return task


def save(task: SeeTask) -> SeeTask:
    """Persist a mutated task (e.g. after planner replan)."""
    return _save(task, since_n=0)  # full event log flush on external mutate


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
    _save(task, since_n=0)  # flush full plan/create event trail
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
        return decide("ABORT", "UNKNOWN_TASK", "ABORTED", blocking=["unknown task"])
    n = len(task.event_log)
    dec = engine.apply_event(task, event)
    _save(task, since_n=n)
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
        return decide("ABORT", "UNKNOWN_TASK", "ABORTED", blocking=["unknown task"])
    prev = task.current_state
    n = len(task.event_log)
    dec = engine.tick(task)
    _save(task, since_n=n)
    if dec.new_state == "STALLED" and prev != "STALLED":
        _notify_see(task, "SEE stalled",
                    f"{task.goal[:80]}\n{dec.feedback or task.stall_reason or ''}")
    return dec


def request_verify(task_id: str) -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return decide("ABORT", "UNKNOWN_TASK", "ABORTED", blocking=["unknown task"])
    n = len(task.event_log)
    dec = engine.request_verify(task)
    _save(task, since_n=n)
    if dec.action == "COMPLETE":
        _emit_memory(task)
        _notify_see(task, "SEE task complete",
                    f"✅ {task.goal[:120]}\nEvidence ok · `{task.id}`")
        try:
            from argus import metrics
            metrics.SEE_COMPLETED.inc()
        except Exception:
            pass
    return dec


def action(task_id: str, action_name: str, *, reason: str = "") -> SupervisorDecision:
    task = get(task_id)
    if not task:
        return decide("ABORT", "UNKNOWN_TASK", "ABORTED", blocking=["unknown task"])
    n = len(task.event_log)
    dec = engine.supervisor_action(task, action_name, reason=reason)
    _save(task, since_n=n)
    if dec.action == "ABORT" or dec.new_state == "ABORTED":
        _notify_see(task, "SEE task aborted",
                    f"🛑 {task.goal[:120]}\n{reason or dec.reason}\n`{task.id}`")
    elif dec.action == "ASK_USER":
        _notify_see(task, "SEE needs you",
                    f"❓ {task.goal[:120]}\n{dec.feedback or reason}\n`{task.id}`")
    return dec


def resume(task_id: str, *, reason: str = "resume from checkpoint") -> dict:
    """Interrupt recovery: load durable task, RESUME if stalled, return brief + last checkpoint.

    Success criterion #4 — no full chat history required; SQLite payload is enough.
    """
    task = get(task_id)
    if not task:
        return {"ok": False, "error": "unknown task"}
    last_cp = task.checkpoints[-1].to_dict() if task.checkpoints else None
    dec = None
    if task.current_state == "STALLED":
        n = len(task.event_log)
        dec = engine.supervisor_action(task, "RESUME", reason=reason)
        _save(task, since_n=n)
    elif task.current_state in ("COMPLETED", "ABORTED"):
        return {
            "ok": False,
            "error": f"task is terminal ({task.current_state})",
            "task_id": task.id,
            "state": task.current_state,
            "last_checkpoint": last_cp,
        }
    # EXECUTING / PLANNING / VERIFYING — already live; just return orientation
    return {
        "ok": True,
        "task_id": task.id,
        "state": task.current_state,
        "action": dec.action if dec else "CONTINUE",
        "last_checkpoint": last_cp,
        "completed_items": list(task.completed_items),
        "missing_evidence": task.missing_evidence(),
        "brief": engine.worker_brief(task),
        "feedback": task.worker_feedback,
    }


def replay_events(task_id: str) -> dict:
    """List append-only events for a task (replayable audit trail — success #7)."""
    task = get(task_id)
    events = _store().list_see_events(task_id, limit=1000)
    return {
        "task_id": task_id,
        "state": task.current_state if task else None,
        "events": events,
        "event_count": len(events),
        "checkpoints": [c.to_dict() for c in (task.checkpoints if task else [])],
    }


def worker_brief(task_id: str) -> str:
    task = get(task_id)
    if not task:
        return ""
    return engine.worker_brief(task)


def prepare_worker_prompt(conversation_id: str | None, user_message: str) -> str:
    """Prefix user message with SEE brief when a task is active (local + GK hosts)."""
    if not conversation_id:
        return user_message or ""
    task = active_for_conversation(conversation_id)
    if not task:
        return user_message or ""
    try:
        tick(task.id)
    except Exception:
        pass
    brief = worker_brief(task.id)
    if not brief:
        return user_message or ""
    return f"{brief}\n\n---\nUser:\n{user_message or ''}"


def _notify_see(task: SeeTask, title: str, message: str) -> None:
    """Sparse HA push (S5). Disabled with ARGUS_SEE_NOTIFY=0."""
    if os.environ.get("ARGUS_SEE_NOTIFY", "1") in ("0", "false", "no"):
        return
    # importance floor for non-terminal noise (stall still notifies — needs attention)
    try:
        import httpx
        base = os.environ.get("HA_URL", "http://nyx:8123").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        if not token:
            return
        service = os.environ.get("ARGUS_NOTIFY_SERVICE", "notify/mobile_app_shanes_iphone")
        # service may be "notify/mobile_app_…" or full path
        path = service if service.startswith("notify/") else f"notify/{service}"
        httpx.post(
            f"{base}/api/services/{path}",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": title[:80], "message": (message or "")[:400]},
            timeout=8,
        )
    except Exception:
        pass


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
