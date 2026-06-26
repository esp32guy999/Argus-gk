# Design: The Work Ledger (durable in-flight job state + self-advancing reconciler)

Status: **proposal** (2026-06-26). Author: Argus pairing session.
Supersedes the ad-hoc `/tmp/*_watch.py` + `systemd-run --user` watcher pattern.

## Why (grounded in how we actually work)

This homelab session is dominated by **many long-running threads in flight at once** —
music pulls (Lidarr/qBit/beets), coder bake-offs, image-model wiring, ComfyUI generations,
downloads. Three pains recur, every session:

1. **Lost background work.** Long jobs live in Bash background jobs or `/tmp` scripts and
   get **killed by session resets**. We keep band-aiding with `systemd-run --user` transient
   units — durable, but invisible and un-queryable.
2. **The "Status?" treadmill.** Shane pings *"Status? / Progress? / Is it finished?"*
   constantly. The real reason isn't curiosity — **it's a manual heartbeat to stop the agent
   stalling** on long work. Each ping forces a full re-probe of Lidarr + qBit + download units
   + transcript. It's a necessary evil *given today's architecture*.
3. **Fabrication.** With no ground-truth job state, a model once **confabulated a git commit**
   (fake hash) and used HA-notify to "reply." Nothing could contradict it.

These are three symptoms of **one missing thing: durable state that lives outside the
conversation, and a loop that advances it without a human poking.**

## What — two halves

### Half A — the Ledger (state)
A `jobs` table in the existing `Store` (SQLite, WAL, same single-connection+lock style as
`messages`). Every long-running thing registers a row and updates it. This is the single
source of truth for "what is in flight."

### Half B — the Reconciler (motion / anti-stall)
A periodic **tick** (asyncio task in `ui/server.py`, alongside `_bind_task_loop`) that, for
each non-terminal job, runs its **probe**, writes the refreshed status, and on any
**transition** (e.g. 99%→done, active→failed) **pushes** a message to chat via the
`publish("chat_message", …)` + `/argus/inject` path we already built. Plus a live status
widget rendered through the inline-HTML embed mechanism we already built.

> Half B is the cure for the "Status?" treadmill: Argus tells *you* when something changes,
> and keeps its own in-flight work moving — you stop being the heartbeat.

## Schema (matches `argus/storage.py` conventions)

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,          -- short ulid/slug, e.g. "dl-leftover-salmon"
    kind        TEXT NOT NULL,             -- 'download'|'bakeoff'|'generation'|'import'|'deploy'|'task'
    title       TEXT NOT NULL,             -- human label for the board
    state       TEXT NOT NULL,             -- see lifecycle below
    progress    REAL,                      -- 0.0–1.0, nullable (indeterminate jobs)
    detail      TEXT,                       -- last status line / error
    probe       TEXT,                       -- JSON: how to refresh truth (see below)
    payload     TEXT,                       -- JSON: kind-specific data
    created_ts  REAL NOT NULL,
    updated_ts  REAL NOT NULL,
    done_ts     REAL,
    conversation_id TEXT                     -- which chat to notify on transition
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, updated_ts);
```

### Lifecycle (state machine)
`proposed → queued → active → (blocked) → done | failed | cancelled`

- `proposed` = the agent suggests a job/plan but **does not run it** — it sits on the board
  awaiting Shane's approve/reject. This is the plan-preview-before-execute step (decision #3).
  Approve → `queued`; reject → `cancelled`.
- `blocked` = waiting on an external availability (e.g. Leftover Salmon: indexer has no
  release). Distinct from `failed` — the reconciler keeps polling `blocked`, gives up `failed`.
- Terminal states (`done|failed|cancelled`) are skipped by the reconciler.

### Probe (the idea that kills fabrication AND "Status?")
A job is **never** "done because the model said so" — only because its **probe** returns truth.
`probe` is JSON describing how the reconciler refreshes the row, e.g.:

```json
{"type": "http_json", "url": "http://localhost:8210/qbt/torrents/info",
 "match": {"name_contains": "70s"}, "progress_path": "progress", "done_at": 1.0}
{"type": "lidarr_artist", "artist_id": 16, "done_at": 1.0}
{"type": "systemd_unit", "unit": "dl-qwenimage", "done_when": "inactive"}
{"type": "comfy_history", "prompt_id": "576c..."}
```

A small registry of probe handlers (one per `type`) does the refresh. New kinds = new handler.

## Surfaces

- **Tool lane `ledger`** (`argus/tools/ledger.py`, with `tests/test_ledger.py` per the
  pre-commit rule): `job_create`, `job_update`, `job_list`, `job_get`, `job_done`. This makes
  the ledger first-class *for the model* — and because `job_done` is gated on the probe, the
  model can't fake completion.
- **Endpoints** (`ui/server.py`): `GET /argus/jobs` (board JSON, refreshed), `GET /argus/jobs/{id}`,
  `POST /argus/jobs` (register), `POST /argus/jobs/{id}/approve`, `.../reject`, `.../cancel`.
  Approve flips a `proposed` job to `queued`; the reconciler picks it up from there.
- **Status widget**: a single inline-HTML embed (reuse `_renderInlineHtml`) that polls
  `/argus/jobs` and renders the live board in chat. "Status?" becomes "scroll up to the board."
- **Reconciler**: `async def _reconcile_loop()` started in lifespan; interval ~20–30s
  (configurable). On transition → `publish("chat_message", …)` so the PWA shows it with no reload.

## What it replaces / integrates
- `/tmp/beets_watch.py`, ad-hoc qBit pollers, `systemd-run --user` watchers → **register a job**
  with a probe instead. The reconciler is the one watcher.
- Music pipeline, bake-offs, ComfyUI generations all become `job_create` calls.
- On **session start**, the agent reads `GET /argus/jobs` instead of re-deriving from transcripts
  — this is what makes `/compact` and `/clear` cheap (the original context-window worry).

## MVP vs later
- **MVP:** `jobs` table + `Store` methods + `GET/POST /argus/jobs` + reconciler with 2 probe
  handlers (`http_json` for qBit, `lidarr_artist`) + transition→chat push + the status widget.
  Migrate the *currently live* work (Leftover Salmon) into it as the first real rows.
- **Later:** the `ledger` tool lane (model-driven), more probe types (systemd, comfy_history,
  generic shell-exit), retry/backoff policy on `blocked`, a ground-truth **action log** table
  (every tool action + real result) feeding the board.

## The four loops, as built (2026-06-26)

"Loop Engineering" names four loop types; Argus now implements all four, mapped onto
the ledger's two job categories:

| Loop type | Where | What it does |
|---|---|---|
| **Heartbeat** | `ledger.reconcile_loop` (~20s tick) | cheap local due-check; picks up new jobs |
| **Cron / scheduled** | `external` jobs + `next_check_ts` | ETA-adaptive: network-probe a download at ETA×0.8 (floor 60s, 300s if ETA unknown), recompute each pass |
| **Hook** | `hooks/stop_ledger_heartbeat.py` (Stop hook) | when the agent stops, continue if there's actionable `agent` work — the "Ralph" loop |
| **Goal** | `payload.success_condition` + probe | iterate until done, **then stop** |

**Goal-loop / ground-truth done (the rule that prevents fake completion):** a job is
`done` only because a hard condition confirms it — an `external` job when its **probe**
says so; an `agent` job's success is checked against its `success_condition` and, where a
probe exists, the probe. The model's word alone never closes a job. The Stop-hook prompt
encodes this: "mark it done only if its probe confirms; if blocked or it needs a human
decision, say so and stop — don't spin."

**Guardrails (all four, per best practice — `argus/loop_guard.py`):** iteration cap
(`MAX_CONSECUTIVE`, rolling window), token budget (`EST_TOKEN_BUDGET`), no-progress reset
(window expiry + stop resets the counter), and an append-only audit log of every decision.
Master switch `ARGUS_LOOP_ENABLED` defaults OFF.

## Resolved decisions (2026-06-26)

1. **Auto-advance: yes, toward goals — but never authorship.** The reconciler *acts* to advance
   a job along its **already-approved goal** when the next step is mechanical and reversible
   (e.g. download hits 100% → kick the beets import; import succeeds → trigger Navidrome rescan).
   It must **not make content/authorship decisions** on its own — it does not decide *what to
   write, commit, or send*. Anything requiring judgment becomes a **`proposed`** job on the board
   for Shane to approve. Proposing new items is explicitly welcome; executing decisions is not.
   This is just the existing scope-control policy (decide-and-notify vs stop-and-ask) applied to
   the reconciler: mechanical in-path advance = act; decision = propose.
2. **Push cadence: every transition.** No batching. Each state change pushes to chat immediately.
3. **Plan-preview before execute.** The agent lays out plans as `proposed` jobs (with the steps
   visible) and waits for approval before running them. The board *is* the approval surface — a
   `proposed` row renders with approve/reject. This is a first-class feature, not just for big
   tasks.

### Notify target
Into the active conversation (`default`) for now; per-job threads are a later refinement.
```
