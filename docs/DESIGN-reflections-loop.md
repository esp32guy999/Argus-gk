# Design: The Reflections Loop (Argus reads its own exhaust and proposes)

Status: **proposal** (2026-06-27). Author: Argus pairing session.
Lineage: steals the "pattern-watch over time, flag without gotcha" idea from a shared
doc of three system-prompts, and bakes in "simple, directional — guidance not truth."
Builds directly on the [[work-ledger]], the proven stop-hook loop, and the proposal queue.

Grounded in two 2026 papers:
- **"From Agent Traces to Trust"** (arXiv 2606.04990) — a survey of *execution provenance*
  (a typed graph of what evidence supported what action) and *evidence tracing*. Shapes the
  **input layer**: read provenance, not flat logs; every observation cites its evidence.
- **REFLEX** (arXiv 2606.16496) — reflective evolution; control-policy domain, but its
  *architecture* transfers: decouple a **Critic** (diagnose → structured, auditable diagnoses)
  from an **Actor** (synthesize → improvements from a persistent skill library), and the
  decoupling is itself the guardrail against noisy/unjustified actions. Shapes the
  **reasoning layer** + the "concrete skill (tool) over abstract lesson" output bias.

Argus already half-embodies both: "a job is done only when its probe confirms" is
evidence-tracing in miniature; "dumb guard + reasoning core" is the Critic/Actor split in
spirit. Reflections is where they fully land.

## Why

Argus now *produces* a trail it never *re-reads*: the `jobs` table, the loop audit log
(`~/.claude/argus-loop-audit.jsonl`), `docs/ISSUES.md`, the Prometheus counters
(`NO_PROGRESS`, `AGENT_TURNS{error,exhausted}`), the conversation store, the session
docs, `known-traps.md`. Every one of those is exhaust — written once, read never. The
patterns that matter to how we actually work are sitting in that exhaust, unread.

We already proved the point by hand: the "Status?" treadmill was a *recurring pattern* we
noticed manually and turned into a memory + the whole ledger. The Reflections loop is that
move, automated: notice the pattern, then do something useful with it.

## What

A scheduled, low-frequency pass that asks one question — **"what keeps happening?"** —
reads the exhaust, and surfaces a *few* honest observations. Two flavors:

1. **Stuck / recurring problems → a gentle heads-up.**
   *"Leftover Salmon has been `blocked` at 33% across ~40 probes for 6 days — the indexers
   don't have the rest. Stop monitoring it?"*
   *"argus-ui crash-looped twice this week, both schema/config — the migration-verify
   discipline isn't sticking."*

2. **Repeated manual flows → a proposed automation.** (the high-value flavor)
   *"You've had me grab music 5 times, same flow each time"* → files a **`proposed` job**
   on the work board: *"Build a `grab <genre> <decade>` tool?"* You approve → the stop-hook
   loop builds it.

Flavor 2 closes every loop we built this week:
**pattern-watch → proposal queue → work-ledger → self-continuing loop.**
The system notices its own friction and proposes its own next feature.

## How it reuses what already exists

| Need | Existing component |
|---|---|
| Cadence / trigger | the reconciler (`argus/ledger.py`) schedules a `reflect` agent job |
| Doing the work | agent-category job + the proven stop-hook loop (reasoning carries it) |
| Output surface | `/argus/inject` + the inline-HTML board ("Reflections" card) |
| Automation proposals | `proposed` job state + `/argus/jobs/{id}/approve` (the approval queue) |
| Persistence | a feedback-type memory for the strongest recurring patterns |
| Voice | `soul.md` — the "no gotcha", directional tone |
| Safety floor | `loop_guard` caps + audit |

No new primitives. Reflections is a *configuration* of the harness, exactly like the three
prompts in the source doc are configurations of a folder+agent.

## Inputs — a provenance digest, read-only (per arXiv 2606.04990)

A deterministic collector builds a compact **provenance digest** — NOT raw dumps, and NOT
flat log lines. Each entry is an evidence-linked record: *observation → the trace that
supports it*, so anything Reflections later says can point back at a citation.

Sources, each reduced to evidence units:
- **jobs table** — per job: state, transition history, time-in-state, blocked/failed counts
  (each citable as `job:<id>@<ts>`).
- **loop audit log** — continue/stop decisions, iteration streaks, cap hits (`audit:<line>`).
- **`ISSUES.md`** — deferral count per item (deferred 4× ≠ optional, it's *avoided*).
- **metrics** — `NO_PROGRESS`, agent error/exhausted rates, tool-selection skew.
- **conversation store** — clusters of similar user asks (the automation signal).
- **session docs / known-traps / postmortems** — recurring failure classes.

The collector is plain code (deterministic) and emits provenance, not prose. The *judgment*
is the model's — but every judgment must trace to a digest citation, or it doesn't ship.

## The reasoning pass — Critic → Actor (per REFLEX, arXiv 2606.16496)

Decoupled into two stages; the decoupling is the primary noise guardrail (their finding):

**1. Critic — diagnose only, no fixes.** Reads the provenance digest and distills
*structured, auditable diagnoses*: `{pattern, evidence:[citations], confidence, recurrence_n}`.
It does NOT suggest actions. Its one job is "what genuinely recurred, and what proves it."
A diagnosis with no evidence citations is dropped. A diagnosis under the recurrence
threshold is dropped. Most runs, the Critic emits little or nothing.

**2. Actor — synthesize from the surviving diagnoses.** Takes only the diagnoses that
cleared the Critic and decides what to *do* with each: `flag` (gentle heads-up) or `automate`
(propose a tool). The Actor draws on the existing **skill library = Argus's tool lanes** —
REFLEX's "reusable snippets over abstract lessons": prefer proposing a concrete tool over
writing a vague "you do X a lot" memory. Output: `{diagnosis_ref, flavor, suggestion}`.

Separating "what's happening" (Critic, evidence-bound) from "what to do about it" (Actor)
means a noisy or unjustified *proposal* can't form unless a real, cited *diagnosis* exists
first. Same shape as `loop_guard` (dumb floor) + reasoning (smart core), one level up.

## Outputs

- **flag** → one "Reflections" card posted to chat (batched, not one-per-pattern). Each
  observation shows its **evidence citation** (`job:dl-leftover-salmon@…`, `audit:…`) so it's
  checkable, not just assertable.
- **automate** → a `proposed` agent job ("Build X?") on the board, inert until approved. The
  proposal carries the diagnosis + evidence so you approve with the receipts in front of you.
- **strong recurring fact** → a `feedback` memory — but bias toward proposing a *tool* over
  storing a *lesson* (REFLEX: concrete skill > abstract lesson).
- **always** → append to `docs/reflections/<date>.md` (never delete — steal from the source).

Argus never *acts* on a pattern. It flags, proposes, and remembers. Shane approves.

## The one design rule that makes it good, not noise

**A high bar — mostly silent.** It earns the right to speak only when a pattern has
genuinely recurred past a threshold (the same temporal-voting discipline as the
spaghetti-watchdog: K occurrences before it counts). A reflection that fires every day is
noise; one that fires when there's *actually* a pattern is gold.

And **directional, not a verdict** (steal #2): every observation carries a confidence and
hedged language. It treats its own logs as *guidance, not truth* — the same humility as the
loop guardrails. "This looks like X, worth a look" — never "X is broken."

Two *structural* guardrails back this up (not just a threshold the model could fudge):
- **Evidence-gating** (paper 1): a diagnosis with no provenance citation is dropped before
  it can become an observation. No receipt, no reflection.
- **Critic/Actor decoupling** (REFLEX): a proposal can't form unless a cited diagnosis
  survived the Critic first. The model can't shortcut straight from a hunch to a "build this."

## Guardrails / safety

- Inputs are **read-only**. The only side effects are: post a card, create `proposed`
  (inert) jobs, write a memory/reflections note. Nothing executes without approval.
- Bounded output (≤5 observations), batched into one card.
- Runs under `loop_guard` caps + audit like any agent job.
- Off by default; opt-in cadence (see below).

## Cadence

Weekly to start (Sunday-night reflect). On-demand via a `reflect now` trigger. NOT daily —
daily can't accumulate enough signal to clear the bar, and would train you to ignore it.

## MVP vs later

- **MVP (the Critic only, read-only — prove the value):** the provenance collector + a
  one-shot Critic run that posts evidence-cited *diagnoses* to a Reflections card. No Actor,
  no proposals, no memory writes — just *see what it surfaces, with receipts*. If the
  diagnoses are sharp and the citations check out, the rest is wiring. This is also the
  cheapest way to test the recurrence threshold (tune until it's mostly silent).
- **Then — add the Actor:** the `proposed`-job automation flavor (the killer feature), the
  tool-over-lesson output bias, memory writes, the weekly schedule.

## What the MVP runs taught us (2026-06-27)

Ran the Critic on both real exhaust (`scripts/critic_pass.py`) and the week's code
(`scripts/code_critic.py`). Three findings that change the design:

1. **Evidence-gating works — and is necessary but NOT sufficient.** Across every run, 0
   phantom citations: the floor never let an invented location through. But on the code
   review, all 3 grounded findings were false-positives-or-nits on triage (one misread an
   intended counter-reset as data loss). The floor proves *where*, not *whether*. So the
   pipeline needs a third stage: **Critic → Verify → Actor.** Each grounded finding gets an
   adversarial check ("real defect, or intended?") before it can become a proposal. (Not yet
   built — the human played Verifier this round.) **Verify is a NON-COLLAPSIBLE gate**
   (SAW; `docs/POLICY-ownership-matrix.md`): it cannot be skipped even when the loop runs
   autonomously — grounded ≠ correct, so nothing acts on a finding that hasn't been verified.

2. **The designated auditor is `qwen3-next-80b`.** The `qwen3-coder-30b` is **benched from
   evaluation** — it under-fires (returned 0 on exhaust AND 0 on code where the 80b found
   real, grounded items). Clean role split: **30b is the DOER** (writes/edits code via the
   `code_edit` lane), **80b is the JUDGE** (Critic/auditor). A good coder is not a good critic.

3. **The collector is the real lever, not the model.** The first exhaust run found nothing
   because a naive `LIMIT 30` let the genuine recurring patterns age out. Aggregation /
   intent-clustering in the digest matters more than model choice.

## Open questions for Shane

1. **Cadence** — weekly, or only on-demand at first?
2. **Automation autonomy** — should an approved "build X" proposal run fully autonomously
   via the loop, or always stop for a plan-review first? (I lean plan-review for anything
   that writes code.)
3. **Scope of the digest** — include the conversation store (richest automation signal, but
   the most data), or start with just jobs + audit + ISSUES?
