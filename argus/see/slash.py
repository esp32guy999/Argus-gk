"""Parse /task slash commands for the UI and HTTP API."""
from __future__ import annotations

import re
from typing import Any

from . import api, planner


HELP = """**`/task`** — Supervisory Execution Engine (SEE)

Start a supervised multi-step task with measurable success criteria. The worker
executes; the supervisor tracks stalls, loops, and evidence-gated completion.

**Start**
```
/task <goal>
/task <goal>
criteria:
- measurable outcome 1
- measurable outcome 2
checklist:
- step a
- step b
```
Or paste JSON: `/task {"goal":"…","success_criteria":["…"]}`

**Control**
- `/task status` — active task for this conversation
- `/task status <id>` — specific task
- `/task list` — recent tasks
- `/task verify` — request verification (needs evidence)
- `/task verify <id>`
- `/task abort` / `/task abort <id>`
- `/task resume` / `/task replan`
- `/task brief` — worker brief (checklist + supervisor feedback)
- `/task events` / `/task events <id>` — replayable event log
- `/task -help` — this text
"""


def _parse_cmd(text: str) -> tuple[str, str]:
    """Return (verb, rest) for `/task …`."""
    raw = (text or "").strip()
    raw = re.sub(r"^/task\b", "", raw, flags=re.I).strip()
    if not raw or raw in ("-help", "--help", "help", "-h"):
        return "help", ""
    # verb rest
    m = re.match(
        r"^(status|list|verify|abort|resume|replan|brief|events|start|new)\b\s*(.*)$",
        raw, re.I | re.S,
    )
    if m:
        return m.group(1).lower(), (m.group(2) or "").strip()
    return "start", raw


def handle_task_command(text: str, *, conversation_id: str | None = None) -> dict[str, Any]:
    """Execute a /task command. Returns a JSON-serializable result for the UI."""
    verb, rest = _parse_cmd(text)

    if verb == "help":
        return {"ok": True, "kind": "help", "markdown": HELP}

    if verb == "list":
        from argus.storage import get_store
        tasks = get_store().list_see_tasks(
            conversation_id=conversation_id, include_terminal=True, limit=15,
        )
        if not tasks:
            return {"ok": True, "kind": "list", "tasks": [], "markdown": "_No SEE tasks yet._"}
        lines = ["**SEE tasks**", ""]
        for t in tasks:
            st = t.get("current_state") or "?"
            gid = t.get("id") or "?"
            goal = (t.get("goal") or "")[:80]
            lines.append(f"- `{gid}` · **{st}** · {goal}")
        return {"ok": True, "kind": "list", "tasks": tasks, "markdown": "\n".join(lines)}

    def _resolve_id(explicit: str) -> str | None:
        if explicit and explicit.startswith("see-"):
            return explicit.split()[0]
        if conversation_id:
            t = api.active_for_conversation(conversation_id)
            if t:
                return t.id
        if explicit:
            # maybe bare id without see- prefix
            cand = explicit.split()[0]
            if api.get(cand):
                return cand
        return None

    if verb == "status":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task. Start one with `/task <goal>`."}
        task = api.get(tid)
        if not task:
            return {"ok": False, "error": f"Unknown task {tid!r}."}
        missing = task.missing_evidence()
        lines = [
            f"**SEE** `{task.id}` · **{task.current_state}**",
            f"**Goal:** {task.goal}",
            "",
            "**Success criteria**",
        ]
        covered = task.criteria_covered()
        for c in task.success_criteria:
            lines.append(f"- {'✅' if c in covered else '⬜'} {c}")
        if task.checklist:
            lines.append("")
            lines.append("**Checklist**")
            for c in task.checklist:
                lines.append(f"- {'✅' if c in task.completed_items else '⬜'} {c}")
        if missing:
            lines.append("")
            lines.append("**Missing evidence:** " + "; ".join(missing))
        if task.worker_feedback:
            lines.append("")
            lines.append(f"_Supervisor:_ {task.worker_feedback}")
        if task.stall_reason:
            lines.append(f"_Stall:_ {task.stall_reason}")
        return {
            "ok": True, "kind": "status", "task_id": task.id,
            "state": task.current_state, "task": task.to_dict(),
            "markdown": "\n".join(lines),
        }

    if verb == "brief":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task."}
        brief = api.worker_brief(tid)
        return {"ok": True, "kind": "brief", "task_id": tid, "markdown": f"```\n{brief}\n```"}

    if verb == "events":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task."}
        rep = api.replay_events(tid)
        lines = [f"**SEE events** `{tid}` · state `{rep.get('state')}` · n={rep['event_count']}", ""]
        for e in rep.get("events") or []:
            lines.append(f"- `{e.get('type')}` {e.get('detail') or ''}")
        if rep.get("checkpoints"):
            lines.append("")
            lines.append("**Checkpoints:** " + ", ".join(
                c.get("label") or "?" for c in rep["checkpoints"]))
        return {"ok": True, "kind": "events", "task_id": tid, "markdown": "\n".join(lines),
                "replay": rep}

    if verb == "verify":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task to verify."}
        dec = api.request_verify(tid)
        task = api.get(tid)
        icon = "✅" if dec.action == "COMPLETE" else "⚠️"
        return {
            "ok": dec.ok or dec.action == "COMPLETE",
            "kind": "verify",
            "task_id": tid,
            "action": dec.action,
            "state": dec.new_state or (task.current_state if task else None),
            "markdown": f"{icon} **VERIFY** → `{dec.action}` ({dec.new_state})\n\n{dec.feedback or dec.reason}",
        }

    if verb == "abort":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task to abort."}
        dec = api.action(tid, "ABORT", reason="user /task abort")
        return {
            "ok": True, "kind": "abort", "task_id": tid,
            "markdown": f"🛑 Task `{tid}` aborted.",
        }

    if verb == "resume":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task."}
        out = api.resume(tid, reason="user /task resume")
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error") or "resume failed"}
        cp = out.get("last_checkpoint") or {}
        cp_line = f"\nLast checkpoint: **{cp.get('label')}**" if cp.get("label") else ""
        return {
            "ok": True, "kind": "resume", "task_id": tid,
            "markdown": (
                f"▶️ Resume `{tid}` → `{out.get('state')}`{cp_line}\n\n"
                f"```\n{out.get('brief') or ''}\n```"
            ),
        }

    if verb == "replan":
        tid = _resolve_id(rest)
        if not tid:
            return {"ok": False, "error": "No active SEE task."}
        # Optional new intent after "replan"
        if rest and not rest.startswith("see-"):
            plan = planner.plan_from_intent(rest)
            task = api.get(tid)
            if task and plan.success_criteria:
                from argus.see import engine
                # load mutable task, apply replan + new plan
                engine.supervisor_action(task, "REPLAN", reason="user replan")
                engine.accept_plan(
                    task,
                    checklist=plan.checklist,
                    success_criteria=plan.success_criteria,
                    required_evidence=plan.required_evidence,
                )
                api.save(task)
                return {
                    "ok": True, "kind": "replan", "task_id": tid,
                    "plan": plan.to_dict(),
                    "markdown": (
                        f"🔄 **Replanned** `{tid}` → EXECUTING\n\n"
                        f"**Goal:** {plan.goal}\n"
                        + "\n".join(f"- {c}" for c in plan.success_criteria)
                    ),
                }
        dec = api.action(tid, "REPLAN", reason="user /task replan")
        return {
            "ok": True, "kind": "replan", "task_id": tid,
            "markdown": f"🔄 REPLAN → `{dec.new_state}`\n\n{dec.feedback or ''}",
        }

    # start / new
    plan = planner.plan_from_intent(rest)
    errs = plan.validate()
    if errs:
        return {"ok": False, "error": "; ".join(errs), "plan": plan.to_dict()}

    task = api.create(
        plan.goal,
        success_criteria=plan.success_criteria,
        checklist=plan.checklist,
        required_evidence=plan.required_evidence,
        constraints=plan.constraints,
        importance=plan.importance,
        conversation_id=conversation_id,
        start=True,
    )
    lines = [
        f"🎯 **SEE task started** `{task.id}`",
        f"**Goal:** {task.goal}",
        f"**State:** {task.current_state} · importance {task.importance}",
        "",
        "**Success criteria** (need evidence before COMPLETE):",
    ]
    for c in task.success_criteria:
        lines.append(f"- ⬜ {c}")
    if task.checklist and task.checklist != task.success_criteria:
        lines.append("")
        lines.append("**Checklist:**")
        for c in task.checklist:
            lines.append(f"- ⬜ {c}")
    lines.append("")
    lines.append(
        "_Worker: use tools, `see_checkpoint` / `see_add_evidence`, then "
        "`see_request_verify` or `/task verify`. You cannot self-declare success._"
    )
    return {
        "ok": True,
        "kind": "start",
        "task_id": task.id,
        "state": task.current_state,
        "plan": plan.to_dict(),
        "task": task.to_dict(),
        "markdown": "\n".join(lines),
    }
