# Scope Control & Deferred Issue Policy

The operative summary lives in `CLAUDE.md`; this is the full reference. The point is
behavioral, not bureaucratic: **stay anchored to the current objective, and don't let
discovery turn into a side quest.**

## Core principle
**Discovery does not equal authorization.** Finding a problem is not permission to solve
it. You may identify, record, and prioritize issues, but may only *work* on something that:
- is part of the authorized objective, or
- removes an obstacle blocking it, or
- is explicitly approved by the user, or
- is scheduled in a planning/review phase.

## 1. Task Anchoring
At the start of any significant task, establish — and **state aloud** — a Task Anchor:

```
objective:         <one line>
success_condition: <testable: how we'll know it's done>
out_of_scope:      [<the obvious non-goals>]
```

- State it so the user can correct `out_of_scope` *before* you act — the pre-commitment is
  what resists later rationalization.
- **Re-anchor** whenever the user gives a new objective; the old anchor is dead.
- If the objective can't be written as a *testable* success condition, that's the signal to
  clarify scope with the user first, not to start.
- Before any action, ask: *does this directly advance the success condition?* If no, it's an
  Obstacle or an Issue (below).

## 2. Obstacle vs Issue
**Obstacle** — directly prevents completing the objective (missing dependency, broken import,
invalid endpoint, corrupted required dataset, wrong config the task needs). Obstacles are
**part of the authorized task**; resolve them — subject to Decision Authority (§4) and the
Circuit Breaker (§6).

**Issue** — a problem found during execution that is **not required** to complete the
objective (refactor, doc gap, perf, architectural concern, naming, debt). **Record and defer.**
Discovery does not authorize work.

## 3. Issue classification
- **Blocking** — objective can't complete safely/correctly without it (security hole,
  data-corruption risk, invalid architecture assumption, missing critical dependency).
- **Deferred** — should happen later; doesn't block now (cleanup, refactor, docs, perf).
- **Observation** — may warrant future evaluation; no action now (scaling, alternatives).

**Bias rule:** unsure Blocking vs Deferred → **default Deferred.** Blocking requires clear
evidence the objective fails without intervention.

## 4. Decision authority
**Decide & notify** (act, then tell the user) when ALL hold: change is reversible · blast
radius Low · it advances the objective or clears an obstacle.
*e.g. fix a broken import, extract a required archive, replace an invalid file, abandon junk data.*

**Stop & ask** when ANY holds: irreversible · blast radius High · multiple viable paths ·
requirements ambiguous · the Task Anchor is no longer valid.
*e.g. schema redesign, framework swap, data migration, architectural pivot.*

If reversibility is **uncertain, treat it as irreversible.**

## 5. Blast radius
- **Low** — localized, minimal impact (rename, doc, logging, small bug fix).
- **Medium** — multiple files/modules/workflows (API change, config change, query change).
- **High** — architecture or multiple dependent systems (schema redesign, framework swap).

## 6. Circuit breaker — the hard stop
Applies to issue investigation **and** obstacle resolution. Trip it after **3 consecutive
actions that neither reduce uncertainty nor advance the `success_condition`.**

Count by *progress*, not by steps or clock. "Number of steps" alone is gameable — one action
can burn 20 minutes — so the real signal is whether each action **narrows the problem** (rules
out a hypothesis, localizes the cause, confirms a fix). The counter **resets only when an
action actually reduces uncertainty**; three fruitless ones in a row means you're spinning,
however the work was chunked. On trip:
- stop, record findings as Deferred, return to the anchor;
- if something you classified as a quick *obstacle* blows past its budget, it's bigger than
  the task → **stop and ask** (the obstacle may be larger than the objective).

Trip early — don't wait for the third — if these show: repeated hypothesis-testing without
progress, effort drifting outside the anchor, or discovery of additional unrelated work. The
purpose of investigation is to unblock execution, not start a new project. The trigger is a
**count of fruitless steps, not a judgment call**; "this is reasonable" is exactly the
rationalization it exists to stop.

## 7. Trivial inline fix exception
Fix a discovered issue immediately **only if ALL hold**: same file you're already editing ·
same subsystem · < 5 minutes · Low blast radius · no new files · no new dependencies · no
impact outside the objective. If any condition fails → **record and continue.** ("It's just
one line" is how scope creep starts — keep this narrow.)

## 8. Issue register
Maintain **exactly one** authoritative register: **`docs/ISSUES.md`.** The memory system is
for behavioral facts, not backlog; session-log "Open / next" *points at* ISSUES.md, never
duplicates it.

**Tiered rigor — match overhead to stakes:**
- Low blast radius → one line: `- [ ] <title> — <category> — <where> (from: <task>, <date>)`
- Medium/High → full record:

```
title:
category:          Blocking | Deferred | Observation
severity:          Low | Medium | High | Critical
status:            open | deferred | resolved | wontfix
blast_radius:      Low | Medium | High
component:
discovered_during:
notes:
```

**Severity** = impact if left unfixed (Low minor · Medium meaningful · High significant
risk/degraded · Critical threatens project). **Blast radius** = cost/reach of the fix. Two
different axes — don't conflate them.

## 9. Review & aging — only at task/milestone boundaries, never mid-task
- **Triage order:** Severity → Blast Radius → Dependency Impact → Expected Value. Never
  discovery order.
- **Aging:** drop obsolete/indirectly-resolved, merge duplicates, mark resolved via `status`,
  promote Observations that became actionable.

## 10. Close the loop
At task/milestone completion, **surface what you deferred** (the new ISSUES.md entries) so the
user can triage. Don't let issues accumulate unseen.

## Directive
Anchor to the objective. Don't chase side quests. Don't confuse discovery with authorization.
Remove obstacles, record findings, finish the task — then decide what deserves attention next.
