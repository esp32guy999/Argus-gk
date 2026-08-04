"""Worker-facing SEE tools — request checkpoints, evidence, verification.

The worker never declares final success; it requests VERIFY via see_request_verify.
"""
from __future__ import annotations

from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool


def see_start_task(
    goal: str,
    success_criteria: str,
    checklist: str = "",
    importance: int = 60,
    conversation_id: str = "",
) -> dict:
    """Start a supervised task (SEE). success_criteria and checklist are newline- or
    semicolon-separated measurable items. Returns task_id and worker brief. Use for
    multi-step work that must not silently stall or fake completion. Pass conversation_id
    when known so tool events bind to this chat."""
    if not (goal or "").strip():
        raise ModelRetry("see_start_task: provide a non-empty goal.")
    crit = [x.strip() for x in (success_criteria or "").replace(";", "\n").splitlines() if x.strip()]
    if not crit:
        raise ModelRetry("see_start_task: provide at least one success criterion line.")
    checks = [x.strip() for x in (checklist or success_criteria).replace(";", "\n").splitlines() if x.strip()]
    from argus.see import api
    cid = (conversation_id or "").strip() or None
    task = api.create(
        goal.strip(),
        success_criteria=crit,
        checklist=checks or crit,
        required_evidence=crit,
        importance=int(importance),
        conversation_id=cid,
        start=True,
    )
    if cid:
        api.bind_conversation(cid, task.id)
    return {
        "task_id": task.id,
        "state": task.current_state,
        "brief": api.worker_brief(task.id),
    }


def see_checkpoint(task_id: str, label: str, checklist_item: str = "") -> dict:
    """Record meaningful progress (checkpoint) on a SEE task. Optionally mark a
    checklist item complete."""
    if not task_id or not label:
        raise ModelRetry("see_checkpoint: task_id and label required.")
    from argus.see import api
    dec = api.checkpoint(
        task_id, label,
        checklist_item=checklist_item or None,
    )
    out = dec.to_dict()
    out["ok"] = dec.ok
    out["feedback"] = dec.feedback  # legacy human line
    return out


def see_add_evidence(
    task_id: str,
    criterion: str,
    summary: str,
    kind: str = "other",
    payload: str = "",
    command: str = "",
    source: str = "",
) -> dict:
    """Attach observable evidence for a success criterion (command output, HTTP status,
    file path, etc.). Prefer tool outputs (kind=http|docker|command|file) with the
    actual command/result in summary/payload. Worker-only claims are weak. Required
    before verification can COMPLETE."""
    if not task_id or not criterion or not summary:
        raise ModelRetry("see_add_evidence: task_id, criterion, and summary required.")
    from argus.see import api
    src = (source or "").strip() or None
    if not src and kind in ("http", "docker", "command", "file", "test"):
        src = f"tool:{kind}"
    dec = api.add_evidence(
        task_id, criterion, summary,
        kind=kind or "other",
        payload=payload or None,
        source=src,
        command=command or None,
    )
    out = dec.to_dict()
    out["ok"] = dec.ok
    return out


def see_request_verify(task_id: str) -> dict:
    """Ask the supervisor to VERIFY the task. Do NOT claim success yourself — only
    the supervisor can COMPLETE after checking evidence vs success criteria."""
    if not task_id:
        raise ModelRetry("see_request_verify: task_id required.")
    from argus.see import api
    dec = api.request_verify(task_id)
    out = dec.to_dict()
    out["ok"] = dec.ok
    out["feedback"] = dec.feedback
    out["completed"] = dec.action == "COMPLETE"
    return out


def see_status(task_id: str = "") -> dict:
    """Get SEE task state and worker brief. If task_id empty, returns hint only."""
    from argus.see import api
    if not task_id:
        return {"hint": "Pass task_id from see_start_task."}
    task = api.get(task_id)
    if not task:
        raise ModelRetry(f"see_status: unknown task {task_id!r}")
    return {
        "task_id": task.id,
        "state": task.current_state,
        "goal": task.goal,
        "completed_items": task.completed_items,
        "missing_evidence": task.missing_evidence(),
        "brief": api.worker_brief(task_id),
        "feedback": task.worker_feedback,
    }


def see_resume(task_id: str) -> dict:
    """Resume a supervised task after interruption or stall from last checkpoint.
    Reloads durable state — no chat history required."""
    if not task_id:
        raise ModelRetry("see_resume: task_id required.")
    from argus.see import api
    out = api.resume(task_id)
    if not out.get("ok"):
        raise ModelRetry(out.get("error") or "resume failed")
    return out


def see_report_step(
    task_id: str,
    action: str,
    reason: str = "",
    code: str = "",
    evidence_json: str = "",
    confidence: float = -1.0,
    tool: str = "",
) -> dict:
    """Report one supervised worker-step decision (SEE response contract).

    Every /task step MUST end with exactly one decision. Never empty.
    action: CONTINUE|RETRY|REPLAN|ASK_USER|STALL|ABORT|COMPLETE|NO_OP.
    NO_OP = evaluated, nothing to do this step (intentional idle).
    COMPLETE requests supervisor VERIFY (you cannot self-complete).
    evidence_json: optional JSON list of {criterion, summary, kind?, ...}.
    """
    if not task_id:
        raise ModelRetry("see_report_step: task_id required.")
    if not (action or "").strip():
        raise ModelRetry(
            "see_report_step: action is required — use NO_OP if nothing to do "
            "(empty action violates the response contract)."
        )
    import json
    evidence = []
    if (evidence_json or "").strip():
        try:
            evidence = json.loads(evidence_json)
        except json.JSONDecodeError as e:
            raise ModelRetry(f"see_report_step: evidence_json is not valid JSON: {e}") from e
        if not isinstance(evidence, list):
            raise ModelRetry("see_report_step: evidence_json must be a JSON list")
    step = {
        "protocol": "STP-1.1",
        "action": action.strip(),
        "reason": reason or "",
        "evidence": evidence,
    }
    if (code or "").strip():
        step["code"] = code.strip()
    if (tool or "").strip():
        step["tool"] = tool.strip()
    if confidence is not None and float(confidence) >= 0:
        step["confidence"] = float(confidence)
    from argus.see import api
    dec = api.report_step(task_id, step)
    out = dec.to_dict()
    out["ok"] = dec.ok
    out["feedback"] = dec.feedback
    out["no_op"] = dec.action == "NO_OP"
    out["completed"] = dec.action == "COMPLETE"
    out["contract"] = "ok" if dec.code != "MALFORMED_STEP" else "failed"
    return out


def tools() -> list[Tool]:
    common = dict(provider="see")
    return [
        Tool(
            name="see_start_task",
            description=("Start a supervised multi-step task (SEE). Provide goal, "
                         "newline-separated measurable success_criteria, optional checklist. "
                         "Returns task_id. Use for long work that needs stall detection and "
                         "evidence-gated completion."),
            tags=["see", "task", "supervisor", "plan", "checklist", "autonomous"],
            func=see_start_task,
            example={
                "goal": "Confirm Sonarr is healthy on glassgarden",
                "success_criteria": "container running\nport 8989 responds",
            },
            **common,
        ),
        Tool(
            name="see_checkpoint",
            description="Record SEE progress checkpoint; optionally complete a checklist item.",
            tags=["see", "checkpoint", "progress"],
            func=see_checkpoint,
            example={"task_id": "see-abc", "label": "compose updated", "checklist_item": "edit compose"},
            **common,
        ),
        Tool(
            name="see_add_evidence",
            description=("Add observable evidence for a SEE success criterion "
                         "(HTTP code, command output, path). Required before verify."),
            tags=["see", "evidence", "verify", "proof"],
            func=see_add_evidence,
            example={
                "task_id": "see-abc",
                "criterion": "port 8989 responds",
                "summary": "HTTP 200 from :8989/ping",
                "kind": "http",
            },
            **common,
        ),
        Tool(
            name="see_request_verify",
            description=("Request SEE verification. Supervisor COMPLETE only if all "
                         "criteria have evidence. Never self-declare success."),
            tags=["see", "verify", "complete", "done"],
            func=see_request_verify,
            example={"task_id": "see-abc"},
            **common,
        ),
        Tool(
            name="see_status",
            description="SEE task status, missing evidence, and supervisor feedback.",
            tags=["see", "status", "progress"],
            func=see_status,
            example={"task_id": "see-abc"},
            **common,
        ),
        Tool(
            name="see_resume",
            description=("Resume a stalled or interrupted SEE task from durable state / "
                         "last checkpoint. Use after crashes or long idle."),
            tags=["see", "resume", "checkpoint", "recover"],
            func=see_resume,
            example={"task_id": "see-abc"},
            **common,
        ),
        Tool(
            name="see_report_step",
            description=(
                "Report one SEE worker-step decision (response contract). "
                "Every supervised step must call this with a required action — "
                "never end a step empty. Use action=NO_OP when nothing to do. "
                "COMPLETE requests verification; NO_OP is intentional idle."
            ),
            tags=["see", "task", "decision", "no_op", "contract", "step", "protocol"],
            func=see_report_step,
            example={
                "task_id": "see-abc",
                "action": "NO_OP",
                "reason": "No changes required.",
                "confidence": 1.0,
            },
            **common,
        ),
    ]
