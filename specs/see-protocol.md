# Supervisory Task Protocol (STP) v1.1

> **Status:** Implemented (runtime `PROTOCOL_ID = "STP-1.1"`; accepts `STP-1.0`)  
> **Location:** `argus/see/models.py`, `argus/see/contract.py`  
> Philosophy: small protocol, stable vocabulary, deterministic supervisor, rich metadata.

Workers and Supervisors treat this as a **versioned network protocol**, not merely Python classes.

---

## Protocol identity

Every decision includes:

```json
"protocol": "STP-1.1"
```

- Workers **SHOULD** require `protocol` and reject incompatible majors (`STP-2.x`).
- Emitters always set `STP-1.1` (parsers still accept `STP-1.0`).
- Changing ACTIONS or CODES is a **protocol revision** (spec + code + acceptance tests).

### v1.1 delta

- **`NO_OP` action** — *state was evaluated and no mutation was required*
  (not “nothing happened”, not empty, **not progress**).
- **Activity vs progress** — `last_activity_ts` (worker responding, includes NO_OP)
  vs `last_progress_ts` (checkpoint / evidence / successful tool). Idle stall uses
  **progress** only so NO_OP spam cannot mask a stuck task.
- **NO_OP accumulation** — consecutive NO_OPs: 0–5 normal, 6–10 review flag,
  **>10 → STALL / NO_PROGRESS** (`ARGUS_SEE_NOOP_REVIEW`, `ARGUS_SEE_NOOP_STALL`).
- **Worker step events** — immutable `WorkerStepReported` with `step_id`.
- **Worker step response contract** (`see_report_step` trust boundary):
  every supervised step yields **exactly one** decision object. Empty / null /
  malformed JSON / missing `action` → rejected (`MALFORMED_STEP`), never success.
- **`message`** — preferred human-readable field (`reason` / `explanation` aliases).

---

## Action vocabulary (9)

```
CONTINUE | RETRY | REPLAN | VERIFY_FAILED | ASK_USER | STALL | ABORT | COMPLETE | NO_OP
```

| Action | State | Meaning |
|--------|-------|---------|
| CONTINUE | EXECUTING | Healthy progress |
| RETRY | EXECUTING | Repeat step; plan still valid |
| REPLAN | PLANNING | Plan cannot succeed |
| VERIFY_FAILED | EXECUTING | Verification denied |
| ASK_USER | STALLED | Need human |
| STALL | STALLED | No progress / loop |
| ABORT | ABORTED | Unrecoverable |
| COMPLETE | COMPLETED | Criteria independently verified |
| NO_OP | EXECUTING (or STALL if accumulated) | Evaluated; no mutation required — activity only |

**Prohibited as actions:** DONE, WAIT, SUCCESS, TRY_AGAIN, STOP, FAIL, ERROR.  
Human/CLI layer may normalize synonyms (`NOOP`/`IDLE`/`SKIP` → `NO_OP`); Supervisor emitters must use canonical values only.

---

## Worker step response contract

```json
{
  "protocol": "STP-1.1",
  "action": "NO_OP",
  "code": "NO_OP",
  "message": "Configuration already satisfies requirements.",
  "confidence": 1.0,
  "evidence": []
}
```

| Field | Required | Notes |
|-------|----------|--------|
| protocol | SHOULD | Emitters set `STP-1.1` |
| action | **YES** | Canonical only (includes `NO_OP`) |
| code | SHOULD | Registry only; defaults per action |
| message | SHOULD | Human-readable (`reason`/`explanation` aliases) |
| evidence | MAY | Criterion-oriented proof objects |
| confidence | NO | Telemetry only — never drives control |
| tool | MAY | Optional tool name hint |

**Invariant:** the orchestrator never accepts empty / null / EOF as a step result.
Use `action=NO_OP` when **evaluation** found no mutation necessary — not when the
worker is idle, confused, or stuck.

`COMPLETE` on a worker step **requests VERIFY** — the worker never self-completes.

### Activity vs progress

| Clock | Updated by | Stall? |
|-------|------------|--------|
| `last_activity_ts` | Any worker response including NO_OP, tool call | No |
| `last_progress_ts` | Checkpoint, evidence, successful tool, plan accept | **Yes** — idle stall |

### NO_OP accumulation

| Consecutive NO_OP | Policy |
|-------------------|--------|
| 0–5 | Normal evaluation heartbeats |
| 6–10 | `details.review=true` — supervisor should inspect |
| ≥11 (`ARGUS_SEE_NOOP_STALL`, default 11) | Escalate to `STALL` / `NO_PROGRESS` |

### Event: WorkerStepReported

```json
{
  "type": "WorkerStepReported",
  "detail": "NO_OP",
  "data": {
    "step_id": "a1b2c3d4e5f6",
    "action": "NO_OP",
    "message": "…",
    "consecutive_no_ops": 4
  }
}
```

---

## Frozen code registry

Unknown codes are **rejected** (same as unknown actions).

| Group | Codes |
|-------|--------|
| Progress | PROGRESS_DETECTED, CHECKPOINT_ACCEPTED, WORKER_ACTIVE, PLAN_ACCEPTED, EVENT_APPLIED, RESUMED, NO_OP, STEP_ACCEPTED, MALFORMED_STEP |
| Verification | MISSING_EVIDENCE, WEAK_EVIDENCE, UNRELATED_EVIDENCE, CRITERION_NOT_MET, VERIFICATION_FAILED, ALL_CRITERIA_MET, VALIDATION_FAILED |
| Planning | PLAN_INVALID, RESOURCE_MISSING, PRECONDITION_FAILED, NEW_INFORMATION |
| Execution | COMMAND_FAILED, TEMPORARY_FAILURE, TIMEOUT, LOOP_DETECTED, NO_PROGRESS, WAITING_FOREVER |
| User | MISSING_INFORMATION, AMBIGUOUS_REQUEST, PERMISSION_REQUIRED, CONFIGURATION_UNKNOWN |
| Terminal | UNRECOVERABLE_ERROR, MAX_RETRIES, SECURITY_POLICY, USER_CANCELLED, UNKNOWN_TASK, ILLEGAL_STATE, PROTOCOL_ERROR |

---

## Decision schema

```json
{
  "protocol": "STP-1.0",
  "action": "VERIFY_FAILED",
  "code": "UNRELATED_EVIDENCE",
  "state": "EXECUTING",
  "blocking": [
    "Expected Sonarr evidence",
    "Received Jellyfin evidence"
  ],
  "details": { "criterion": "sonarr container running" },
  "confidence": 0.98
}
```

| Field | Required | Notes |
|-------|----------|--------|
| protocol | SHOULD (emitters always set) | Version |
| action | YES | ENUM only |
| code | YES | Registry only |
| state | YES | SEE state machine |
| blocking | SHOULD | Actionable list — what to fix |
| details | MAY | Structured data |
| confidence | NO | **Telemetry only** — MUST NOT drive execution |

Unknown fields SHOULD be ignored (forward compatibility).

---

## Evidence provenance

```json
{
  "criterion": "sonarr port responds",
  "kind": "http",
  "summary": "curl HTTP 200 from http://localhost:8989/ping",
  "payload": "...",
  "source": "tool:curl",
  "command": "curl -s -o /dev/null -w '%{http_code}' http://localhost:8989/ping",
  "trust": 0.95,
  "ts": 0
}
```

- Prefer `source: "tool:…"` over `worker:claim`.
- VERIFY evaluates whether evidence **proves the criterion** (relevance + quality), not mere existence.
- Worker-authored `other` claims without command are weak.

---

## Worker requirements

- Accept only the 8 actions and registered codes.
- Reject unknown protocol / action / code / state / missing required fields.
- Obey `action`; never invent COMPLETE.
- Use `blocking` for recovery; use `confidence` only for logs.

## Supervisor requirements

- Emit exactly one valid action + registry code + next state.
- Explain every blocker in `blocking`.
- Never COMPLETE without verification.
- Deterministic for identical inputs (rules-based engine).

---

## Human UX vs protocol

Chat/CLI may accept “retry”, “resume”, “try again” and map to canonical actions.
Logs, tests, and worker APIs use STP values only.

---

## Multi-worker (future)

Add metadata only (`worker_id`, `attempt`, `parent_task`, …) — **do not** expand ACTIONS.

---

## Implementation map

| Piece | Path |
|-------|------|
| PROTOCOL_ID, ACTIONS, CODES, SupervisorDecision | `argus/see/models.py` |
| Engine emissions | `argus/see/engine.py` |
| Worker tool payloads | `argus/tools/see_tools.py` → `dec.to_dict()` |
| Acceptance | `tests/test_see.py`, `tests/test_see_acceptance.py` |
