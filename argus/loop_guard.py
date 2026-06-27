"""Loop guardrails — the safety core every self-continuing loop needs (the field
calls these non-negotiable: iteration cap, budget cap, no-progress detection,
audit trail). Used by the Stop-hook auto-continue ("Ralph") loop, but kept pure
and standalone so it's unit-testable and reusable.

The decision is deliberately conservative: CONTINUE only when there is genuine,
agent-owned, actionable work AND every guardrail passes. Anything ambiguous ->
STOP and hand control back to the human. A loop that errs toward stopping wastes
a turn; one that errs toward continuing burns money and talks over the user.

State + audit live in plain files so a hook process (separate from the server) can
read/write them with no DB coupling:
  ~/.claude/argus-loop-state.json   {consecutive, window_start, est_tokens}
  ~/.claude/argus-loop-audit.jsonl  append-only, one decision per line
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, asdict

# --- config (env-overridable; the master switch defaults OFF) --------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default

ENABLED = os.environ.get("ARGUS_LOOP_ENABLED", "0") == "1"   # master switch, OFF by default
MAX_CONSECUTIVE = _env_int("ARGUS_LOOP_MAX_CONSECUTIVE", 25)  # hard cap on auto-continues
WINDOW_SEC = _env_int("ARGUS_LOOP_WINDOW_SEC", 3600)          # rolling window for the cap
EST_TOKEN_BUDGET = _env_int("ARGUS_LOOP_TOKEN_BUDGET", 4_000_000)  # rough ceiling per window

_STATE_DIR = os.path.expanduser(os.environ.get("ARGUS_LOOP_DIR", "~/.claude"))
_STATE_FILE = os.path.join(_STATE_DIR, "argus-loop-state.json")
_AUDIT_FILE = os.path.join(_STATE_DIR, "argus-loop-audit.jsonl")

# An agent job is "actionable" when it's mine to push and not waiting on something.
_ACTIONABLE_STATES = ("queued", "active")


@dataclass
class Decision:
    cont: bool          # True -> auto-continue; False -> let the agent stop
    reason: str         # human-readable why (audited + shown)
    prompt: str | None  # the pre-written nudge to inject when cont=True
    job_id: str | None = None


def _load_state(now: float) -> dict:
    try:
        s = json.load(open(_STATE_FILE))
    except (OSError, ValueError):
        s = {}
    # Reset the rolling window if it has elapsed (no-progress windows expire).
    if now - s.get("window_start", 0) > WINDOW_SEC:
        s = {"consecutive": 0, "window_start": now, "est_tokens": 0}
    s.setdefault("consecutive", 0)
    s.setdefault("window_start", now)
    s.setdefault("est_tokens", 0)
    return s


def _save_state(s: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, _STATE_FILE)


def _audit(entry: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def actionable_jobs(jobs: list[dict]) -> list[dict]:
    """Agent-category jobs that are mine to push right now: an approved (not
    'proposed') agent job in a live, non-blocked state. Probe-owned/external jobs
    and blocked/terminal ones are NOT actionable — the reconciler/world owns those."""
    return [j for j in jobs
            if j.get("category") == "agent"
            and j.get("state") in _ACTIONABLE_STATES]


def _make_prompt(job: dict) -> str:
    title = job.get("title", job.get("id", "the current task"))
    sc = (job.get("payload") or {}).get("success_condition")
    tail = f" Success condition: {sc}" if sc else ""
    return (f"Continue working on the active ledger job: {title} "
            f"(id={job.get('id')}).{tail} If it's genuinely done, mark it done "
            f"(its probe must confirm) and stop. If you're blocked or it needs a "
            f"human decision, say so and stop — don't spin.")


def decide(jobs: list[dict], *, user_waiting: bool = False, now: float | None = None,
           est_session_tokens: int | None = None) -> Decision:
    """Pure decision: given the current ledger jobs, should the agent auto-continue?
    CONTINUE only if enabled, the user isn't owed a reply, there's actionable agent
    work, and neither the iteration cap nor the token budget is exceeded.

    `est_session_tokens`, when supplied by the caller (the Stop hook measures it from
    the live transcript), is the real budget signal — gate on it directly. Falls back to
    the windowed `est_tokens` counter only when no live measurement is available."""
    now = time.time() if now is None else now
    if not ENABLED:
        return Decision(False, "loop disabled (ARGUS_LOOP_ENABLED!=1)", None)
    if user_waiting:
        return Decision(False, "user message unanswered — yielding", None)
    todo = actionable_jobs(jobs)
    if not todo:
        return Decision(False, "no actionable agent work pending", None)
    state = _load_state(now)
    if state["consecutive"] >= MAX_CONSECUTIVE:
        return Decision(False, f"iteration cap hit ({MAX_CONSECUTIVE}/window)", None, todo[0].get("id"))
    tokens = est_session_tokens if est_session_tokens is not None else state["est_tokens"]
    if tokens >= EST_TOKEN_BUDGET:
        return Decision(False, f"token budget hit (~{EST_TOKEN_BUDGET})", None, todo[0].get("id"))
    job = todo[0]
    return Decision(True, "actionable agent work; guardrails OK", _make_prompt(job), job.get("id"))


def record(decision: Decision, *, est_tokens_added: int = 0, now: float | None = None) -> dict:
    """Commit a decision: bump/reset the counters and append the audit line. Returns
    the new state. Call this once per Stop-hook invocation after decide()."""
    now = time.time() if now is None else now
    state = _load_state(now)
    if decision.cont:
        state["consecutive"] += 1
        state["est_tokens"] += max(0, est_tokens_added)
    else:
        state["consecutive"] = 0          # a real stop resets the no-progress counter
    _save_state(state)
    _audit({"ts": round(now, 1), "cont": decision.cont, "reason": decision.reason,
            "job_id": decision.job_id, "consecutive": state["consecutive"],
            "est_tokens": state["est_tokens"]})
    return state
