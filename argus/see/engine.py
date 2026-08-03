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
    decide,
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
    return decide("CONTINUE", "WORKER_ACTIVE", "PLANNING")


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
    task.worker_feedback = (
        "Execute the checklist. Request verification when success criteria have evidence."
    )
    return decide(
        "CONTINUE", "PLAN_ACCEPTED", "EXECUTING",
        blocking=[],
        details={"checklist": task.checklist, "success_criteria": task.success_criteria},
    )


def apply_event(task: SeeTask, event: SeeEvent | dict) -> SupervisorDecision:
    """Ingest one event; update task; may change state (e.g. loop → STALLED)."""
    if task.current_state in TERMINAL:
        return decide(
            "ABORT", "ILLEGAL_STATE", task.current_state,
            blocking=[f"Task is {task.current_state}; no further events."],
        )
    ev = event if isinstance(event, SeeEvent) else SeeEvent.from_dict(event)
    if ev.type not in EVENT_TYPES and ev.type != "StateTransition":
        ev = SeeEvent(type=ev.type, detail=ev.detail, data={**ev.data, "unknown_type": True}, ts=ev.ts)

    _append_event(task, ev)
    now = time.time()

    if ev.type == "ToolCalled":
        tool = ev.detail or ev.data.get("tool") or "tool"
        args_key = ev.data.get("args_key") or json.dumps(ev.data.get("args") or {}, sort_keys=True, default=str)
        task.tool_history.append({"tool": tool, "args_key": args_key, "ts": ev.ts, "ok": None})
        recent = [h for h in task.tool_history[-20:] if h["tool"] == tool and h["args_key"] == args_key]
        if len(recent) >= LOOP_REPEAT and task.current_state == "EXECUTING":
            _append_event(task, SeeEvent(type="LoopDetected", detail=tool, data={"count": len(recent)}))
            _append_event(task, _transition(task, "STALLED", f"loop:{tool}"))
            task.worker_feedback = (
                f"loop detected on {tool} (×{len(recent)}). "
                f"Change approach, mark a checkpoint, or request REPLAN."
            )
            return decide(
                "STALL", "LOOP_DETECTED", "STALLED",
                blocking=[f"Repeated tool {tool} ×{len(recent)} with identical arguments"],
                details={"tool": tool, "count": len(recent)},
            )

    if ev.type in ("ToolCompleted", "ToolFailed"):
        ok = ev.type == "ToolCompleted"
        if task.tool_history:
            task.tool_history[-1]["ok"] = ok
        if ok:
            task.last_progress_ts = now
        elif ev.type == "ToolFailed":
            return decide(
                "RETRY", "COMMAND_FAILED", task.current_state,
                blocking=[f"Tool {ev.detail or 'unknown'} failed"],
                details={"tool": ev.detail, "error": ev.data.get("error")},
            )

    if ev.type == "CheckpointReached":
        label = ev.detail or ev.data.get("label") or "checkpoint"
        task.checkpoints.append(Checkpoint(label=label, detail=ev.data.get("detail")))
        task.last_progress_ts = now
        item = ev.data.get("checklist_item")
        if item and item in task.checklist and item not in task.completed_items:
            task.completed_items.append(item)
            _append_event(task, SeeEvent(type="ChecklistItemDone", detail=item))
        return decide(
            "CONTINUE", "CHECKPOINT_ACCEPTED", task.current_state,
            details={"label": label, "checklist_item": item},
        )

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

    if task.current_state == "STALLED" and ev.type in (
        "CheckpointReached", "EvidenceAdded", "ChecklistItemDone", "ToolCompleted",
    ):
        _append_event(task, _transition(task, "EXECUTING", f"progress:{ev.type}"))
        task.worker_feedback = "progress observed; resumed EXECUTING."
        return decide(
            "CONTINUE", "RESUMED", "EXECUTING",
            details={"via": ev.type},
        )

    return decide(
        "CONTINUE", "PROGRESS_DETECTED" if ev.type in (
            "ToolCompleted", "CheckpointReached", "EvidenceAdded", "ChecklistItemDone",
        ) else "EVENT_APPLIED",
        task.current_state,
        details={"event": ev.type},
    )


def tick(task: SeeTask, *, now: float | None = None) -> SupervisorDecision:
    """Periodic stall check — call from a timer or between worker turns."""
    if task.current_state in TERMINAL:
        return decide("CONTINUE", "EVENT_APPLIED", task.current_state)
    if task.current_state not in ("EXECUTING", "PLANNING", "VERIFYING"):
        return decide("CONTINUE", "EVENT_APPLIED", task.current_state)

    now = now if now is not None else time.time()
    idle = now - (task.last_progress_ts or task.last_event_ts or task.created_ts)
    if task.current_state == "EXECUTING" and idle >= IDLE_STALL_SEC:
        _append_event(task, SeeEvent(
            type="StallDetected", detail="idle",
            data={"idle_sec": idle, "threshold": IDLE_STALL_SEC},
        ))
        _append_event(task, _transition(task, "STALLED", f"idle:{idle:.0f}s"))
        code = "WAITING_FOREVER" if idle >= IDLE_STALL_SEC * 3 else "NO_PROGRESS"
        blocking = [
            f"No checkpoints for {idle:.0f} seconds",
            "No measurable progress",
        ]
        task.worker_feedback = (
            f"idle stall ({idle:.0f}s without progress). "
            f"Continue with a new action, add a checkpoint, or request REPLAN."
        )
        # Long idle also escalates to ASK_USER semantics via code; action stays STALL
        # unless extremely long — protocol: STALL for no progress; ASK_USER for missing info.
        action = "ASK_USER" if idle >= IDLE_STALL_SEC * 3 else "STALL"
        if action == "ASK_USER":
            code = "MISSING_INFORMATION"
            blocking.append("Supervisor needs user or replan guidance after prolonged idle")
        return decide(
            action, code, "STALLED",
            blocking=blocking,
            details={"idle_sec": idle, "threshold": IDLE_STALL_SEC},
        )
    return decide("CONTINUE", "WORKER_ACTIVE", task.current_state)


_WEAK_EVIDENCE = frozenset({
    "ok", "done", "yes", "yep", "passed", "pass", "success", "fine", "good",
    "works", "working", "complete", "completed", "true", "1", "✓", "✅",
    "service running", "it works", "all good",
})

# Generic words that do not prove a specific criterion by themselves.
_EVIDENCE_STOP = frozenset({
    "the", "a", "an", "is", "to", "for", "of", "and", "or", "with", "on", "in",
    "at", "be", "must", "should", "that", "this", "file", "path", "contains",
    "exists", "check", "verify", "contents", "content", "size", "test", "true",
    "false", "status", "show", "shows", "from", "via", "using", "returned",
    "exact", "exactly", "match", "matched", "output", "result", "results",
    "service", "running", "up", "active", "ok", "done", "pass", "passed",
    "config", "configuration", "xml", "json", "yml", "yaml", "etc", "tmp",
    "home", "usr", "var", "opt", "mnt", "data", "log", "logs",
})

# Known homelab/service names — mismatch between criterion and evidence is a fail.
_KNOWN_SERVICES = frozenset({
    "sonarr", "radarr", "lidarr", "prowlarr", "readarr", "jellyfin", "plex",
    "emby", "navidrome", "qbittorrent", "transmission", "sabnzbd", "nginx",
    "caddy", "traefik", "grafana", "prometheus", "homeassistant", "hass",
    "pihole", "unbound", "wireguard", "openvpn", "gluetun", "postgres",
    "mariadb", "redis", "argus", "immich", "syncthing", "n8n",
})


_WEAK_VERBS = frozenset({
    "responds", "respond", "running", "started", "exists", "contains",
    "listening", "working", "healthy", "available", "reachable", "created",
    "modified", "updated", "installed", "deployed", "verified", "checked",
    "container", "port",  # structural words — subject is the service/name/number
    "written", "marked", "configured", "enabled", "disabled", "present",
    "absent", "failed", "succeed", "successful", "ready", "complete",
})


def _significant_tokens(text: str) -> set[str]:
    """Subject tokens evidence must address (services, ports, filenames)."""
    out: set[str] = set()
    text = text or ""
    out |= set(re.findall(r"\b(\d{2,5})\b", text))
    for m in re.findall(r"[\w.-]+\.(?:txt|xml|yml|yaml|json|conf|cfg|md|log)", text, re.I):
        out.add(m.lower())
    for svc in _KNOWN_SERVICES:
        if re.search(rf"\b{re.escape(svc)}\b", text, re.I):
            out.add(svc)
    for t in re.findall(r"[a-z][a-z0-9_-]{3,}", text.lower()):
        if t in _EVIDENCE_STOP or t in _WEAK_VERBS or t in _KNOWN_SERVICES:
            continue
        if len(t) >= 5:
            out.add(t)
    return out


def _evidence_proves_criterion(criterion: str, summary: str, payload: str | None,
                               kind: str) -> list[str]:
    """Return issue strings if evidence does not prove this criterion."""
    issues = []
    blob = f"{summary or ''}\n{payload or ''}"
    crit = criterion or ""
    blob_l = blob.lower()

    summary_s = (summary or "").strip()
    if len(summary_s) < 12:
        issues.append(f"evidence too thin for {crit!r}: {summary_s!r}")
        return issues
    if summary_s.lower() in _WEAK_EVIDENCE:
        issues.append(
            f"evidence for {crit!r} is a bare claim ({summary_s!r}); "
            f"need observable detail (command output, HTTP status, path, …)"
        )

    if kind == "http" and not re.search(
        r"\b([1-5]\d{2}|http|https|curl|status|respond)\b", blob, re.I
    ):
        issues.append(
            f"http evidence for {crit!r} should mention status/URL/response"
        )
    if kind == "docker" and not re.search(
        r"\b(docker|container|up|running|compose|health)\b", blob, re.I
    ):
        issues.append(
            f"docker evidence for {crit!r} should mention container/state"
        )

    for q in re.findall(r"[\"']([^\"']{2,})[\"']", crit):
        if q.lower() not in blob_l:
            issues.append(
                f"evidence does not prove {crit!r}: missing quoted content {q!r}"
            )

    sig = _significant_tokens(crit)
    ports = {t for t in sig if t.isdigit()}
    names = sig - ports
    if ports and not any(p in blob_l for p in ports):
        if not re.search(r"\b(port|curl|http|listen|:)\b", blob_l, re.I):
            issues.append(
                f"evidence does not prove {crit!r}: required port(s) "
                f"{sorted(ports)} not present in evidence"
            )
    if names and not any(n in blob_l for n in names):
        issues.append(
            f"evidence does not prove {crit!r}: none of the key terms "
            f"{sorted(names)} appear in the evidence (unrelated artifact?)"
        )

    crit_svc = {s for s in _KNOWN_SERVICES if re.search(rf"\b{re.escape(s)}\b", crit, re.I)}
    evid_svc = set()
    for svc in _KNOWN_SERVICES:
        if re.search(rf"(?:^|[/_\s.-]){re.escape(svc)}(?:[/_\s.-]|$)", blob_l):
            evid_svc.add(svc)
    if crit_svc and evid_svc and not (crit_svc & evid_svc):
        issues.append(
            f"evidence does not prove {crit!r}: evidence refers to "
            f"{sorted(evid_svc)} but criterion is about {sorted(crit_svc)}"
        )

    if re.search(r"\bservice\s+running\b", blob_l) and crit_svc:
        if not any(s in blob_l for s in crit_svc):
            issues.append(
                f"evidence does not prove {crit!r}: generic "
                f"'service running' does not identify {sorted(crit_svc)}"
            )

    return issues


def _evidence_quality_issues(task: SeeTask) -> list[str]:
    """VERIFY: reject hollow claims and evidence that does not prove its criterion."""
    issues = []
    for e in task.evidence:
        issues.extend(_evidence_proves_criterion(
            e.criterion, e.summary, e.payload, e.kind or "other",
        ))
    return issues


def request_verify(task: SeeTask) -> SupervisorDecision:
    """Worker believes it is done — SEE returns COMPLETE or VERIFY_FAILED."""
    if task.current_state in TERMINAL:
        return decide(
            "ABORT", "ILLEGAL_STATE", task.current_state,
            blocking=[f"Task already {task.current_state}"],
        )
    if task.current_state == "STALLED":
        _append_event(task, _transition(task, "EXECUTING", "verify_from_stall"))
    if task.current_state not in ("EXECUTING", "VERIFYING"):
        return decide(
            "RETRY", "ILLEGAL_STATE", task.current_state,
            blocking=[f"verify only from EXECUTING (now {task.current_state})"],
        )

    _append_event(task, _transition(task, "VERIFYING", "worker_request"))
    _append_event(task, SeeEvent(type="VerificationStarted", detail="check evidence"))

    missing = task.missing_evidence()
    incomplete = [c for c in task.checklist if c not in task.completed_items]
    weak = _evidence_quality_issues(task)

    if missing or incomplete or weak:
        blocking: list[str] = []
        code = "VERIFICATION_FAILED"
        if missing:
            blocking.append("missing evidence for: " + "; ".join(missing))
            code = "MISSING_EVIDENCE"
        if incomplete:
            blocking.append("checklist open: " + "; ".join(incomplete))
        if weak:
            for w in weak[:6]:
                blocking.append(w)
            if any("unrelated" in w.lower() or "does not prove" in w.lower() or "jellyfin" in w.lower()
                   or "refers to" in w.lower() for w in weak):
                code = "UNRELATED_EVIDENCE"
            elif any("thin" in w.lower() or "bare claim" in w.lower() for w in weak):
                code = "WEAK_EVIDENCE"
            elif code == "VERIFICATION_FAILED":
                code = "CRITERION_NOT_MET"
        task.worker_feedback = " | ".join(blocking)
        _append_event(task, SeeEvent(
            type="VerificationFailed",
            detail=task.worker_feedback[:500],
            data={
                "missing_evidence": missing,
                "incomplete_checklist": incomplete,
                "weak_evidence": weak,
                "code": code,
            },
        ))
        _append_event(task, _transition(task, "EXECUTING", "verify_failed"))
        return decide(
            "VERIFY_FAILED", code, "EXECUTING",
            blocking=blocking,
            details={
                "missing_evidence": missing,
                "incomplete_checklist": incomplete,
                "weak_evidence": weak[:8],
            },
        )

    _append_event(task, SeeEvent(type="VerificationPassed", detail="all criteria evidenced"))
    _append_event(task, _transition(task, "COMPLETED", "verified"))
    _append_event(task, SeeEvent(type="TaskCompleted", detail=task.goal[:200]))
    task.worker_feedback = "COMPLETED — evidence satisfied success criteria."
    return decide(
        "COMPLETE", "ALL_CRITERIA_MET", "COMPLETED",
        blocking=[],
        details={
            "verified_criteria": len(task.success_criteria),
            "evidence_items": len(task.evidence),
            "checkpoints": len(task.checkpoints),
        },
        confidence=0.98,
    )


def supervisor_action(task: SeeTask, action: str, *, reason: str = "") -> SupervisorDecision:
    """Explicit SEE/user action. Accepts protocol actions + RESUME synonym."""
    from .models import ACTION_SYNONYMS, ACTIONS_SET
    raw = (action or "").upper().strip()
    action = ACTION_SYNONYMS.get(raw, raw)
    # RESUME is synonym for CONTINUE after leaving STALLED
    if raw == "RESUME":
        action = "CONTINUE"

    if action not in ACTIONS_SET and raw not in ("RESUME", "VERIFY"):
        return decide(
            "CONTINUE", "EVENT_APPLIED", task.current_state,
            blocking=[f"unknown action {raw!r} ignored"],
        )

    if raw == "VERIFY" or action == "VERIFY_FAILED":
        if raw == "VERIFY":
            return request_verify(task)

    if action == "ABORT":
        if task.current_state not in TERMINAL:
            _append_event(task, _transition(task, "ABORTED", reason or "abort"))
            _append_event(task, SeeEvent(type="TaskAborted", detail=reason or "abort"))
        return decide(
            "ABORT",
            "USER_CANCELLED" if "user" in (reason or "").lower() else "UNRECOVERABLE_ERROR",
            "ABORTED",
            blocking=[reason or "aborted"],
        )

    if action == "REPLAN":
        if task.current_state in TERMINAL:
            return decide("ABORT", "ILLEGAL_STATE", task.current_state,
                          blocking=["task already terminal"])
        if task.current_state != "PLANNING":
            if task.current_state == "EXECUTING":
                _append_event(task, _transition(task, "STALLED", "replan_prep"))
            _append_event(task, _transition(task, "PLANNING", reason or "replan"))
        _append_event(task, SeeEvent(type="SupervisorAction", detail="REPLAN", data={"reason": reason}))
        task.worker_feedback = "REPLAN — produce a new checklist/plan."
        return decide(
            "REPLAN", "PLAN_INVALID" if reason else "NEW_INFORMATION", "PLANNING",
            blocking=[reason] if reason else ["current plan cannot succeed"],
        )

    if action == "CONTINUE" or raw == "RESUME":
        if task.current_state == "STALLED":
            _append_event(task, _transition(task, "EXECUTING", reason or "resume"))
            task.worker_feedback = "RESUME execution."
            return decide("CONTINUE", "RESUMED", "EXECUTING", details={"reason": reason})
        return decide("CONTINUE", "WORKER_ACTIVE", task.current_state)

    if action == "ASK_USER":
        _append_event(task, SeeEvent(type="SupervisorAction", detail="ASK_USER", data={"reason": reason}))
        task.worker_feedback = reason or "need user input."
        if task.current_state == "EXECUTING":
            _append_event(task, _transition(task, "STALLED", "ask_user"))
        return decide(
            "ASK_USER", "MISSING_INFORMATION", "STALLED",
            blocking=[reason or "user input required"],
        )

    if action == "STALL":
        if task.current_state == "EXECUTING":
            _append_event(task, _transition(task, "STALLED", reason or "stall"))
        return decide("STALL", "NO_PROGRESS", "STALLED", blocking=[reason or "stalled"])

    if action == "RETRY":
        return decide("RETRY", "TEMPORARY_FAILURE", "EXECUTING",
                      blocking=[reason] if reason else [])

    if action == "COMPLETE":
        return request_verify(task)

    return decide(action, "EVENT_APPLIED", task.current_state,
                  blocking=[reason] if reason else [])


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
