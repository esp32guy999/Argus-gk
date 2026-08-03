# SEE Supervisor Decision Protocol (v1)

> Public API between Supervisor and Worker. Immutable within a protocol version.
> Changing the action vocabulary requires: this spec, implementation, and acceptance tests.

## Purpose

The Supervisor does **not** solve the task. It evaluates execution and returns a
**structured decision**. The Worker MUST react and MUST NOT invent new decision types.

## Allowed actions (ENUM)

Exactly one of:

| Action | Next state | Meaning |
|--------|------------|---------|
| `CONTINUE` | EXECUTING | Healthy progress |
| `RETRY` | EXECUTING | Repeat step; plan still valid |
| `REPLAN` | PLANNING | Plan cannot succeed |
| `VERIFY_FAILED` | EXECUTING | Verification denied |
| `ASK_USER` | STALLED | Need human input |
| `STALL` | STALLED | No measurable progress / loop |
| `ABORT` | ABORTED | Unrecoverable / cancelled |
| `COMPLETE` | COMPLETED | All criteria independently verified |

**Forbidden synonyms** (map as follows): WAIT→ASK_USER/STALL, SUCCESS/FINISHED/DONE→COMPLETE,
TRY_AGAIN→RETRY, ERROR→RETRY/ABORT, FAIL→VERIFY_FAILED/ABORT, STOP→ABORT, RESUME→CONTINUE.

## Response schema

```json
{
  "action": "<ACTION>",
  "code": "<SPECIFIC_REASON>",
  "state": "<NEXT_STATE>",
  "blocking": ["..."],
  "details": {},
  "confidence": 0.98
}
```

- `action`, `code`, `state` REQUIRED  
- `blocking` SHOULD list exact blockers  
- `details` MAY hold structured data  
- `confidence` OPTIONAL telemetry only — MUST NOT affect execution  
- Unknown fields SHOULD be ignored (forward compatible)

## Implementation

| Piece | Location |
|-------|----------|
| ENUM + schema | `argus/see/models.py` (`ACTIONS`, `SupervisorDecision`, `decide`) |
| Engine emissions | `argus/see/engine.py` |
| Worker tools return `dec.to_dict()` | `argus/tools/see_tools.py` |

Worker: `SupervisorDecision.from_dict(payload)` rejects unknown actions.
