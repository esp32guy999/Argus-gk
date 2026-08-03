# Supervisory Execution Engine (SEE)

> **Status:** Accepted architecture (2026-08-03) — Version 1 design.  
> **Author:** Argus Project (Shane + pairing).  
> **Objective:** Improve autonomous task reliability by monitoring progress, validating
> evidence, detecting stalls, and guiding execution — **without** replacing the worker model.

Memory flywheel (F0–F2 landed; F3–F5 parked) plugs in as a **downstream consumer** of
SEE evidence, not a parallel supervisor.

---

## Executive summary

Autonomous agents fail in three recurring ways:

1. They **silently stop** making progress.
2. They **repeat** the same actions.
3. They **declare success** without verifying results.

SEE is a second layer that evaluates **execution**, not thoughts:

| Role | Job |
|------|-----|
| **Planner** | Intent → structured task (once, unless replan) |
| **Worker** | Reason + tools (never final authority on success) |
| **SEE** | State, stalls, loops, evidence, transitions |
| **Memory policy** | Importance-gated cold candidates from evidence |

**Supervise state, not thoughts.** Observable events, milestones, evidence, progress —
not prompt critique of chain-of-thought.

---

## Design goals

### SHALL

- Detect stalled execution and repetitive behavior.
- Track measurable task progress (checklist + events).
- Require **evidence** before COMPLETED.
- Trigger CONTINUE / RETRY / REPLAN / ASK_USER / ABORT.
- Stay cheap enough for continuous local use (<250 ms prefer, small model OK).
- Prefer deterministic rules; LLM supervisor only where judgment helps.
- Log every transition (replayable).

### SHALL NOT

- Solve the user’s task.
- Rewrite code or do deep planning.
- Replace the worker model.
- Write **confirmed** long-term memory (candidates only → flywheel).

---

## High-level architecture

```
                 User
                  │
                  ▼
          Planner (LLM)  ── once unless REPLAN
                  │
          Structured Task object
                  │
                  ▼
          Worker (LLM / GK / loop)
                  │
      Tool calls → Events
                  │
                  ▼
    Supervisory Execution Engine
      (state machine + rules [+ optional small model])
                  │
      Continue / Retry / Verify / Replan / Ask / Abort
                  │
                  ▼
          Memory policy (importance)
                  │
                  ▼
      Cold candidates → (later) consolidation
```

---

## Relationship to existing Argus (do not reinvent)

| SEE concept | Already in tree | Role in v1 |
|-------------|-----------------|------------|
| Tool call events | `watchdog`, loop `on_event`, bubble activity | **Emit** into SEE event log |
| Repeat / loop detection | `argus/watchdog.py`, `loop_guard` | **Feed** SEE STALLED/LOOP |
| Anti-announce stall | `loop._looks_unfinished` | One recovery pattern under EXECUTING |
| Long-running jobs | `jobs` table + DESIGN-work-ledger | SEE **tasks** may link to a job id |
| Background runner | `argus/tasks.py` | Optional worker host for long SEE tasks |
| Memory candidates | `memory_policy` + D5 ledger | SEE → `accept_candidate` only |
| Fact lifecycle | `memory_facts` | **Out of SEE** — consolidation later |
| HA notify | `notify_phone` / HA | Sparse: task complete / ask user / high-value memory |

SEE is the **unifying state machine** around work; existing sensors become event sources.

---

## Components

### 1. Planner

- Understand intent; output **structured task** (not freeform essay).
- Define measurable success criteria + initial checklist + required evidence.
- Runs **once** unless SEE issues REPLAN.

### 2. Worker

- Execute; call tools; emit progress.
- **Must not** declare final success.
- Requests transition: e.g. `VERIFY` when it believes done.

### 3. SEE (supervisor)

- Own all state transitions.
- Consume **events** + task object (not full transcript).
- Default path: **deterministic rules**; optional small local model (Gemma/Qwen) for
  ambiguous replan/ask decisions only.

---

## Task object (canonical work unit)

```text
id, goal, success_criteria[], checklist[], completed_items[]
current_state, importance, constraints[], required_evidence[]
tool_history[], event_log[], checkpoints[], evidence[]
conversation_id?, job_id?, created_ts, updated_ts
```

Supervisor **never** rereads the entire conversation — only the task object + new events.

---

## State machine

```
NEW → PLANNING → EXECUTING → VERIFYING → COMPLETED
                      │
                      ├→ STALLED → CONTINUE | REPLAN | ASK_USER | ABORT
                      └→ (from VERIFYING) back to EXECUTING if evidence fails
```

| State | Meaning |
|-------|---------|
| NEW | Task created, not planned |
| PLANNING | Planner running |
| EXECUTING | Worker active |
| VERIFYING | Evidence check vs success criteria |
| COMPLETED | Terminal success |
| STALLED | No progress / loop / idle |
| ABORTED | Terminal failure / user abort |

**SEE owns all transitions.** Worker requests; supervisor decides.

---

## Events (examples)

`TaskCreated`, `TaskStarted`, `PlanGenerated`, `ToolCalled`, `ToolCompleted`,
`ToolFailed`, `CheckpointReached`, `VerificationStarted`, `VerificationPassed`,
`VerificationFailed`, `TaskCompleted`, `TaskAborted`, `SupervisorAction`

---

## Checkpoints

Worker (or tools) create checkpoints after meaningful progress:

```text
Found compose → Modified compose → Restarted container → Health OK
```

Crash recovery: restore task object + latest checkpoint + checklist + evidence.
**No full reasoning history required.**

---

## Progress, stalls, loops

**Progress** = checklist advance, successful tools, verified file/API/test/container state.  
**Not** wall-clock alone.

**Stall signals:**

| Class | Signal |
|-------|--------|
| Idle | No events / tools / checkpoints for N ticks |
| Loop | Repeated same tool+args / same failure / same read |
| No state change | Tools fire but checklist/evidence frozen |

**Supervisor actions:** CONTINUE · RETRY · REPLAN · ASK_USER · ABORT

---

## Evidence & verification

Claims are insufficient. Each success criterion needs evidence, e.g.:

- `docker ps` / health endpoint  
- HTTP status  
- Command output hash  
- Test results  
- File checksum  

Worker → `VERIFY` → SEE checks checklist + evidence. Fail → EXECUTING + feedback.

---

## Memory integration

```
Execution evidence
    → importance policy (existing memory_policy)
    → cold candidate only
    → (future) nightly consolidation
```

SEE **never** writes confirmed facts.

**HA:** task completed / failed needing attention / explicit remember / high-value config —
not every checkpoint.

---

## Observability (v1 metrics)

- tasks started / completed / aborted  
- retries, replans, stalls  
- verification failures  
- supervisor action counts  
- (existing) memory candidates accepted/rejected  

Every transition append-only in task event log (replayable).

---

## Performance goals

| Target | Value |
|--------|--------|
| Supervisor tick | &lt;250 ms preferred (rules path) |
| Token use | Summaries + task object, not full chat |
| Hardware | Continuous on anvil local models |

---

## Version 1 scope

### In

- Task object (SQLite)  
- Event stream  
- State machine + rule-based supervisor  
- Checkpoints  
- Stall / loop detection (integrate watchdog signals)  
- Verification + evidence records  
- Memory candidate hooks (importance)  
- Basic telemetry  

### Out (later)

- Assertion graph, multi-worker, full episodic DB  
- Web auto-confirm, RL, advanced decay  
- Distributed execution  

---

## Success criteria (v1)

1. Task runs planner → execute → verify → COMPLETED (or ABORT) under SEE.  
2. Idle and loop detected without relying on full transcript.  
3. COMPLETED requires evidence, not worker claim alone.  
4. Interrupt + resume from checkpoint.  
5. High-value evidence can enter memory flywheel as **candidates**.  
6. (Soft) After promotion path exists: new session recall without re-research.  
7. All transitions logged and replayable.

---

## Implementation phases

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **S0** | Spec + map to watchdog/jobs | **done** (`specs/see.md`) |
| **S1** | `see_tasks` / `see_events` + pure engine + tests | **done** (`argus/see/`, `tests/test_see.py`) |
| **S2** | Loop tool events → SEE; worker tools; brief inject | **done** (light): `loop` sink + `see_*` tools |
| **S3** | Planner structured task JSON; slash `/task` UI | **done** |
| **S4** | Richer VERIFY policies; multi-turn GK host | pending |
| **S5** | HA on complete/ask; polish memory_policy from evidence | partial (COMPLETED → candidates) |

### Code map (S1–S3)

| Path | Role |
|------|------|
| `argus/see/models.py` | Task, Evidence, Event, states |
| `argus/see/engine.py` | Pure state machine, stall/loop/verify |
| `argus/see/api.py` | Persist + public API |
| `argus/see/planner.py` | Intent → structured TaskPlan (JSON) |
| `argus/see/slash.py` | `/task` command parser + handlers |
| `argus/tools/see_tools.py` | Worker tools: start / checkpoint / evidence / verify / status |
| `argus/storage.py` | `see_tasks`, `see_events` tables |
| `argus/loop.py` | Tool events → active SEE task; worker brief prefix |
| `ui/server.py` | `POST /argus/see/task`, GET tasks |
| `ui/static/app.js` | `/task` slash → handleTask |

**`/task` usage:** `/task <goal>`, sectioned criteria/checklist, JSON plan, `status|list|verify|abort|resume|replan|brief|-help`.

Env: `ARGUS_SEE_IDLE_SEC` (default 120), `ARGUS_SEE_LOOP_REPEAT` (3), `ARGUS_SEE_MEMORY` (1).

---

## Guiding principles

1. Supervise **execution**, not reasoning.  
2. Evidence &gt; assertions.  
3. State transitions &gt; prompt inspection.  
4. Long-term memory is **earned**.  
5. Observable progress defines “intelligence during execution.”  
6. Every autonomous action should be explainable, replayable, recoverable.

---

## Open decisions (need Shane for S1)

1. **Entry point:** every chat? only `/task` / background jobs? only dangerous lanes?  
2. **Supervisor brain:** rules-only for S1–S2, or always a small local model?  
3. **Storage:** new tables in `argus.db` vs separate `see.db`?  
4. **Worker host:** existing Pydantic loop only first, then GK?

**Recommendation:** S1–S2 on **local loop + `/task` opt-in**; rules-first supervisor;
tables in `argus.db`; GK later.
