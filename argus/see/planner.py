"""SEE Planner — intent → structured task plan (JSON-serializable).

v1 is **deterministic rules + light NLP** (no LLM required) so /task is reliable
on local hardware. A future LLM planner can implement the same Plan shape.

Output is the planner half of specs/see.md — consumed by api.create().
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field


@dataclass
class TaskPlan:
    """Structured task definition — planner output / SEE input."""
    goal: str
    success_criteria: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    importance: int = 60
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> list[str]:
        errs = []
        if not (self.goal or "").strip():
            errs.append("goal is required")
        if not self.success_criteria:
            errs.append("at least one success_criterion is required")
        return errs


# Section headers users may type under /task
_SECTION = re.compile(
    r"^(goal|success|success_criteria|criteria|checklist|steps|evidence|"
    r"required_evidence|constraints|constraint|importance|notes)\s*:\s*(.*)$",
    re.I,
)
_NUMBERED = re.compile(r"^\s*(?:\d+[\.)\]]\s+|[-*•]\s+)")
_SPLIT_AND = re.compile(r"\s+(?:and then|then|and)\s+", re.I)


def _lines_to_items(block: str) -> list[str]:
    items = []
    for raw in (block or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = _NUMBERED.sub("", line).strip()
        if line:
            items.append(line[:200])
    # also semicolon-separated single line
    if len(items) == 1 and ";" in items[0]:
        items = [x.strip() for x in items[0].split(";") if x.strip()]
    return items


def _importance_from_text(text: str, default: int = 60) -> int:
    t = (text or "").lower()
    if re.search(r"\b(critical|urgent|production|don'?t break)\b", t):
        return 85
    if re.search(r"\b(homelab|sonarr|radarr|docker|server|glassgarden|anvil)\b", t):
        return 70
    if re.search(r"\b(low priority|whenever|nice to have)\b", t):
        return 40
    return default


def _default_criteria_from_goal(goal: str) -> list[str]:
    """Decompose a one-line goal into measurable-ish criteria."""
    g = goal.strip()
    # Explicit "so that / with / ensuring"
    m = re.search(r"\b(?:so that|ensuring|with)\b\s+(.+)$", g, re.I)
    if m:
        tail = m.group(1).strip()
        parts = _SPLIT_AND.split(tail)
        if len(parts) > 1:
            return [p.strip()[:160] for p in parts if p.strip()][:8]

    # Action patterns: verify/check X → "X is verified"
    m = re.match(
        r"^(?:verify|check|confirm|ensure|test)\s+(.+)$", g, re.I)
    if m:
        subject = m.group(1).strip().rstrip(".")
        return [
            f"{subject} is reachable or observable",
            f"evidence captured for {subject}",
        ]

    m = re.match(r"^(?:install|deploy|set up|setup|start)\s+(.+)$", g, re.I)
    if m:
        subject = m.group(1).strip().rstrip(".")
        return [
            f"{subject} installed or running",
            f"{subject} health or port check passes",
            "configuration saved or recorded",
        ]

    m = re.match(r"^(?:fix|repair|debug)\s+(.+)$", g, re.I)
    if m:
        subject = m.group(1).strip().rstrip(".")
        return [
            f"root cause of {subject} identified",
            f"{subject} fixed or mitigated",
            "fix verified with evidence",
        ]

    # Fallback: single criterion is the goal itself (still measurable only if user refines)
    return [g[:160], "result verified with observable evidence"]


def plan_from_intent(intent: str) -> TaskPlan:
    """Parse freeform or sectioned intent into a TaskPlan."""
    text = (intent or "").strip()
    if not text:
        return TaskPlan(goal="", success_criteria=[])

    # Try full JSON blob
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and (data.get("goal") or data.get("success_criteria")):
                crit = data.get("success_criteria") or data.get("criteria") or []
                if isinstance(crit, str):
                    crit = _lines_to_items(crit)
                check = data.get("checklist") or data.get("steps") or crit
                if isinstance(check, str):
                    check = _lines_to_items(check)
                evid = data.get("required_evidence") or data.get("evidence") or crit
                if isinstance(evid, str):
                    evid = _lines_to_items(evid)
                cons = data.get("constraints") or []
                if isinstance(cons, str):
                    cons = _lines_to_items(cons)
                return TaskPlan(
                    goal=str(data.get("goal") or text)[:300],
                    success_criteria=[str(x)[:200] for x in crit],
                    checklist=[str(x)[:200] for x in check],
                    required_evidence=[str(x)[:200] for x in evid],
                    constraints=[str(x)[:200] for x in cons],
                    importance=int(data.get("importance") or _importance_from_text(text)),
                    notes=str(data.get("notes") or "")[:500],
                )
        except json.JSONDecodeError:
            pass

    # Sectioned form:
    # Goal: ...
    # Criteria:
    # - a
    # Checklist:
    # - b
    sections: dict[str, list[str]] = {}
    current = "goal"
    sections.setdefault("goal", [])
    for raw in text.splitlines():
        m = _SECTION.match(raw.strip())
        if m:
            key = m.group(1).lower()
            if key in ("success", "success_criteria", "criteria"):
                current = "criteria"
            elif key in ("checklist", "steps"):
                current = "checklist"
            elif key in ("evidence", "required_evidence"):
                current = "evidence"
            elif key in ("constraints", "constraint"):
                current = "constraints"
            elif key == "importance":
                current = "importance"
            elif key == "notes":
                current = "notes"
            else:
                current = "goal"
            rest = (m.group(2) or "").strip()
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        sections.setdefault(current, []).append(raw)

    goal_bits = sections.get("goal") or []
    goal = " ".join(x.strip() for x in goal_bits if x.strip())
    # first line only for goal if multi-paragraph without headers
    if not sections.get("criteria") and not sections.get("checklist"):
        # pure freeform: first line goal, rest as notes / steps
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            goal = lines[0]
            rest_items = [_NUMBERED.sub("", ln).strip() for ln in lines[1:]]
            rest_items = [x for x in rest_items if x]
            if rest_items:
                sections["criteria"] = rest_items

    goal = (goal or text.splitlines()[0]).strip()[:300]
    criteria = _lines_to_items("\n".join(sections.get("criteria") or []))
    if not criteria:
        criteria = _default_criteria_from_goal(goal)

    checklist = _lines_to_items("\n".join(sections.get("checklist") or []))
    if not checklist:
        checklist = list(criteria)

    evidence = _lines_to_items("\n".join(sections.get("evidence") or []))
    if not evidence:
        evidence = list(criteria)

    constraints = _lines_to_items("\n".join(sections.get("constraints") or []))
    imp_raw = " ".join(sections.get("importance") or [])
    try:
        importance = int(re.search(r"\d+", imp_raw).group()) if re.search(r"\d+", imp_raw) else _importance_from_text(text)
    except Exception:
        importance = _importance_from_text(text)
    notes = "\n".join(sections.get("notes") or [])[:500]

    return TaskPlan(
        goal=goal,
        success_criteria=criteria[:12],
        checklist=checklist[:16],
        required_evidence=evidence[:12],
        constraints=constraints[:8],
        importance=max(0, min(100, importance)),
        notes=notes,
    )


def plan_to_json(plan: TaskPlan) -> str:
    return json.dumps(plan.to_dict(), indent=2)
