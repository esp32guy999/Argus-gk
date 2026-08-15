"""SS state snapshot — short trajectory, never the full conversation.

Progress fields are emitted by Argus/SEE, not guessed by the model.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TaskSlice:
    id: str = ""
    goal: str = ""
    state: str = ""
    elapsed_seconds: float = 0.0


@dataclass
class ModelSlice:
    name: str = ""
    generating: bool = False
    tokens_per_second: float = 0.0
    seconds_since_token: float = 0.0
    streamed: bool = False  # True only after a real token/activity pulse


@dataclass
class ToolsSlice:
    calls: int = 0
    errors: int = 0
    repeated_calls: int = 0
    last_tool: str = ""


@dataclass
class ProgressSlice:
    new_evidence: bool = False
    state_changed: bool = False
    objective_progress: float = 0.0


@dataclass
class SystemSlice:
    gpu_ok: bool = True
    backend_ok: bool = True
    service: str = ""


def _streamed_from(model_d: dict) -> bool:
    """Bench fixtures omit `streamed`; infer it from a live token clock."""
    if "streamed" in model_d:
        return bool(model_d.get("streamed"))
    return bool(model_d.get("generating") and float(model_d.get("seconds_since_token") or 0) > 0)


@dataclass
class Snapshot:
    """Structured input to SS. Keep it small on purpose."""
    task: TaskSlice = field(default_factory=TaskSlice)
    tools: ToolsSlice = field(default_factory=ToolsSlice)
    progress: ProgressSlice = field(default_factory=ProgressSlice)
    model: ModelSlice = field(default_factory=ModelSlice)
    system: SystemSlice = field(default_factory=SystemSlice)
    history: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "task": asdict(self.task),
            "tools": asdict(self.tools),
            "progress": asdict(self.progress),
            "model": asdict(self.model),
            "system": asdict(self.system),
            "history": list(self.history[-8:]),
        }
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        """Accept the bench/vision shape or a nested Snapshot.to_dict()."""
        if not isinstance(d, dict):
            raise TypeError("snapshot must be a dict")
        # Flat bench shape puts tools/progress at top level (already nested).
        task_d = d.get("task") if isinstance(d.get("task"), dict) else {}
        # Vision doc also had elapsed on task; bench used task_age_sec at top.
        elapsed = float(
            (task_d or {}).get("elapsed_seconds")
            or d.get("task_age_sec")
            or 0
        )
        goal = (task_d or {}).get("goal") or d.get("goal") or ""
        tools_d = d.get("tools") if isinstance(d.get("tools"), dict) else {}
        prog_d = d.get("progress") if isinstance(d.get("progress"), dict) else {}
        model_d = d.get("model") if isinstance(d.get("model"), dict) else {}
        sys_d = d.get("system") if isinstance(d.get("system"), dict) else {}
        hist = d.get("history") if isinstance(d.get("history"), list) else []
        return cls(
            task=TaskSlice(
                id=str((task_d or {}).get("id") or d.get("task_id") or ""),
                goal=str(goal)[:240],
                state=str((task_d or {}).get("state") or d.get("state") or ""),
                elapsed_seconds=elapsed,
            ),
            tools=ToolsSlice(
                calls=int(tools_d.get("calls") or 0),
                errors=int(tools_d.get("errors") or 0),
                repeated_calls=int(tools_d.get("repeated_calls") or 0),
                last_tool=str(tools_d.get("last_tool") or ""),
            ),
            progress=ProgressSlice(
                new_evidence=bool(prog_d.get("new_evidence")),
                state_changed=bool(prog_d.get("state_changed")),
                objective_progress=float(prog_d.get("objective_progress") or 0.0),
            ),
            model=ModelSlice(
                name=str(model_d.get("name") or ""),
                generating=bool(model_d.get("generating")),
                tokens_per_second=float(model_d.get("tokens_per_second") or 0.0),
                seconds_since_token=float(model_d.get("seconds_since_token") or 0.0),
                streamed=_streamed_from(model_d),
            ),
            system=SystemSlice(
                gpu_ok=bool(sys_d.get("gpu_ok", True)),
                backend_ok=bool(sys_d.get("backend_ok", True)),
                service=str(sys_d.get("service") or ""),
            ),
            history=list(hist)[-8:],
            note=str(d.get("note") or ""),
        )


def _repeated_calls(history: list[dict]) -> int:
    """Max times any (tool, args_key) appears in the recent window."""
    counts: dict[tuple[str, str], int] = {}
    for h in history[-20:]:
        key = (str(h.get("tool") or ""), str(h.get("args_key") or ""))
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values(), default=0)


def snapshot_from_task(task: Any, *, extras: dict | None = None, now: float | None = None) -> Snapshot:
    """Build a snapshot from a SeeTask. extras supply live model/system sensors."""
    extras = extras or {}
    tnow = now if now is not None else time.time()
    hist = list(getattr(task, "tool_history", None) or [])
    evidence = list(getattr(task, "evidence", None) or [])
    checklist = list(getattr(task, "checklist", None) or [])
    completed = list(getattr(task, "completed_items", None) or [])
    criteria = list(getattr(task, "success_criteria", None) or [])
    if checklist:
        obj = len(completed) / max(1, len(checklist))
    elif criteria:
        covered = {getattr(e, "criterion", "") for e in evidence}
        obj = len(covered & set(criteria)) / max(1, len(criteria))
    else:
        obj = 0.0
    last_progress = float(getattr(task, "last_progress_ts", 0) or 0)
    last_activity = float(getattr(task, "last_activity_ts", 0) or 0)
    created = float(getattr(task, "created_ts", tnow) or tnow)
    # Evidence added recently (or at all with recent progress) counts as new.
    new_ev = False
    if evidence:
        latest_ev = max(float(getattr(e, "ts", 0) or 0) for e in evidence)
        new_ev = latest_ev >= last_progress - 1e-6 and latest_ev > 0
    errors = sum(1 for h in hist if h.get("ok") is False)
    last_tool = ""
    if hist:
        last_tool = str(hist[-1].get("tool") or "")
    model_x = extras.get("model") if isinstance(extras.get("model"), dict) else extras
    sys_x = extras.get("system") if isinstance(extras.get("system"), dict) else extras
    recent = []
    for h in hist[-5:]:
        recent.append({
            "tool": h.get("tool"),
            "ok": h.get("ok"),
            "ts": h.get("ts"),
        })
    return Snapshot(
        task=TaskSlice(
            id=str(getattr(task, "id", "") or ""),
            goal=str(getattr(task, "goal", "") or "")[:240],
            state=str(getattr(task, "current_state", "") or ""),
            elapsed_seconds=max(0.0, tnow - created),
        ),
        tools=ToolsSlice(
            calls=len(hist),
            errors=errors,
            repeated_calls=_repeated_calls(hist),
            last_tool=last_tool,
        ),
        progress=ProgressSlice(
            new_evidence=new_ev,
            state_changed=abs(last_activity - last_progress) > 1e-6,
            objective_progress=float(obj),
        ),
        model=ModelSlice(
            name=str(model_x.get("name") or extras.get("model_name") or ""),
            generating=bool(model_x.get("generating", extras.get("generating", False))),
            tokens_per_second=float(
                model_x.get("tokens_per_second") or extras.get("tokens_per_second") or 0
            ),
            seconds_since_token=float(
                model_x.get("seconds_since_token") or extras.get("seconds_since_token") or 0
            ),
            streamed=bool(model_x.get("streamed", extras.get("streamed", False))),
        ),
        system=SystemSlice(
            gpu_ok=bool(sys_x.get("gpu_ok", extras.get("gpu_ok", True))),
            backend_ok=bool(sys_x.get("backend_ok", extras.get("backend_ok", True))),
            service=str(sys_x.get("service") or extras.get("service") or ""),
        ),
        history=recent,
        note=str(extras.get("note") or ""),
    )
