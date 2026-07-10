# Argus Memory System — Architecture Outline (for peer review)

> DRAFT 2026-07-10. Written to be shopped to external frontier models as grounded peers.
> Author: Claude (architect) synthesizing a Gemma-steered SOTA research round + the
> existing Argus codebase. Reviewers: please attack the OPEN QUESTIONS (§9) hardest —
> that's where we're least sure.

## 0. What this is
A long-term memory layer for **Argus**, a single-user, privacy-first homelab AI agent.
Goal: the agent recalls relevant facts/preferences/past outcomes across sessions
without (a) melting a 16GB GPU, (b) becoming a prompt-injection vector, or (c)
confidently recalling stale/wrong facts. We are EXTENDING an existing skeleton, not
adopting a memory framework.

## 1. Environment & hard constraints (so critiques are grounded)
- **Compute:** one RTX 5080 (16GB), shared with the chat model. Embeddings run
  **CPU-only** (nomic-embed-text, 768-dim) so they never compete for VRAM.
- **Store:** SQLite (WAL) + `sqlite-vec` for vector search. Single process, low volume.
- **Orchestration:** Argus has its **own agent harness** (tool registry, `select()`,
  watchdog, metrics). We do NOT want a second orchestrator (rules out Letta/MemGPT
  as a framework; we steal its *patterns*, not its runtime).
- **Trust posture:** security-conscious. Content arrives at three trust levels —
  user (high), tool output (medium), web/document (untrusted). This already shapes
  the web lane (nonce provenance envelopes, per-session isolation).

## 2. Existing assets (extend these, don't replace)
- `memory.py`: a `DocMemory` embedded index over curated doc chunks + a `facts`
  table with **supersession** (new fact retires old) and **trust** fields already
  conceived in its docstring.
- `storage.py`: `memory_candidates` ledger (D5) — append-only capture of
  "might be worth remembering" moments (kinds: dead_end, correction, repeat_lookup,
  rule, other). **Currently empty** — it is the empirical dataset write-policy will be
  designed FROM, not guessed.
- The web lane's provenance-envelope + trust discipline, reusable for recalled content.

## 3. Design principles (hard-won, non-negotiable)
1. **Curation is a background pass, not a write-time guess.** SOTA 2026 (sleep-time
   compute / consolidation / algorithmic forgetting) says "what's worth remembering"
   is decided by a periodic consolidation job, not at the moment of capture.
2. **Provenance is first-class.** Every memory carries source + trust. A recalled
   memory is you-from-the-past talking — it triggers *verify*, never blind obey.
3. **Stale beats absent is FALSE.** A confidently-recalled dead endpoint is worse
   than not remembering. Supersession + decay are core, not afterthoughts.
4. **One source of truth per fact.** The store owns facts; prompts mirror.
5. **Seam, not silver bullet.** Build the interface; keep v1 implementations stupid;
   upgrade behind the seam.

## 4. Architecture — a memory hierarchy (the MemGPT pattern, stolen not adopted)
```
 (hot)  Working memory     = current session context (exists: message history)
   ↑ page in on relevance/recency
 (warm) Long-term store    = facts table (structured) + vector index (semantic)
   ↑ promote                     ↑ supersede / decay
 (cold) Candidate ledger   = D5 append-only capture (raw "maybe" moments)
        Consolidation pass = periodic CPU job: dedup → summarize → promote → forget
```
- **Retrieval** = semantic (sqlite-vec top-k) re-ranked by recency × trust, then
  **paged** into the prompt within a token budget (the MemGPT RAM/archival idea).
- **Hybrid, but LIGHT.** Structured `facts` rows carry light metadata (entity,
  key, value, source, trust, ts, superseded_by). A full knowledge graph / multi-hop
  traversal is **deferred** (§9) — entity/relation extraction costs an LLM pass per
  write; not justified for v1 recall needs.

## 5. Write policy / curation (evidence-driven, not designed in a vacuum)
- Phase 0: the ledger fills during real use. NO promotion rules shipped until we
  have ~weeks of real candidates to inspect.
- Promotion (ledger → fact) happens in the consolidation pass, by kind:
  corrections and stated rules promote readily; dead_ends become
  "don't-retry" negative memories; repeat_lookups suggest a fact worth caching.
- Decay: facts unused for N recalls and not marked durable lose trust, then archive.

## 6. Provenance & injection defense (the pillar that's most *us*)
- Memory-injection is a named 2026 threat class (arXiv 2604.16548): untrusted
  content poisons long-term memory → persistent manipulation.
- Defenses: (a) **untrusted content cannot auto-write memory** — web/document text
  is never promoted to a fact without a user/Claude-authorized step; (b) recalled
  content retains its trust tag; low-trust recalls are wrapped in the same
  provenance envelope (quote-only framing) used for web fetches; (c) the
  consolidation pass validates writes (no silent trust escalation).

### 6a. Fact lifecycle — explicit STATE MACHINE (peer review 2026-07-10)
Replaces the ad-hoc trust+supersession pair with one auditable lifecycle. Every
fact carries a state; transitions are logged, never destructive:
`proposed → observed → confirmed → superseded / invalidated`
- **proposed**: asserted once (by a model or an untrusted source). Not yet recalled
  as truth; recall wraps it "unconfirmed".
- **observed**: seen/used again without contradiction.
- **confirmed**: independently corroborated — CRITICALLY, *not* by mere repetition
  from the same/untrusted source (see 6b), only by a trusted signal (user, tool
  ground-truth, or multi-source agreement).
- **superseded**: a newer fact replaces it; the old row is kept (decision provenance).
- **invalidated**: proven wrong; kept as a negative memory ("we believed X, it was
  false") so the mistake isn't silently repeatable.
Contradiction handling falls out of this: conflicting claims coexist as separate
rows; the newer/higher-trust one wins recall, the loser moves to superseded, and the
*history is preserved* so "why did we think X" is always answerable.

### 6b. Missing failure mode the design didn't cover (architect's addition)
We hardened *recall* against injection but left the **consolidation writer** — itself
an LLM summarizing untrusted candidates — as an unguarded injection surface with MORE
authority than recall (it *writes* the long-term store). A poisoned candidate could
manipulate the summarizer into escalating its own trust or deleting a rival fact.
Two consequences: (1) confirmation must never be frequency-based (repeated exposure ≠
truth — this is the "poisoning through repetition" defense); (2) the consolidation
pass runs with the same provenance discipline as recall — untrusted candidate text is
data-not-instructions to the summarizer, and consolidation cannot move a fact to
`confirmed` on untrusted signal alone.

## 7. Consolidation ("the sleep")
Periodic CPU job (cron/idle): dedup near-identical candidates; summarize clusters;
promote per §5; supersede/decay per §3/§5; emit metrics. This is where curation
actually lives — cheap (CPU embeddings, SQL), no VRAM, no model-server bounce.

**Audit & reversal (peer review):** every consolidation action (promote, supersede,
invalidate, summarize, forget) is written append-only to a `consolidation_log` with
before/after + reason. Nothing is hard-deleted — "forget" = archive with a tombstone,
so a wrong promotion or an over-eager forget is *reversible*. Answer to "what's the
recovery path when consolidation errs": replay the log, revert the transition. The
log is also the training data for improving the consolidation policy over time.

## 8. Evaluation (how we prove it helps — the pillar we almost skipped)
- Adopt the *tasks* from LoCoMo / LongMemEval / AMA-Bench (multi-session recall,
  needle-in-a-haystack), run a **local harness** over our own store.
- Metric before/after memory: task success + wrong-recall rate (the stale-fact
  failure must be measured, not assumed away).
- **Relational-failure tagging (peer review):** do NOT pre-build a KG. Instead, tag
  every failed retrieval by cause — is it relational (ownership/dependency/causal/
  temporal-across-events) or not? The KG decision (§9.1) is made from that tally, not
  from principle. Introduce graph edges only if relational failures are a real share.
- Do NOT cite vendor benchmark numbers (e.g. Mem0's LoCoMo 92.5%) as ground truth —
  those are positioning; verify against primary sources.

## 9. OPEN QUESTIONS (reviewers: aim here)
> Peer round 1 (2026-07-10) resolved several by adding §6a state machine (contradiction
> handling, stale retirement, trust evolution), §6b (poisoning-by-repetition), and §7
> audit/reversal. Still genuinely open below.
1. **KG vs light-metadata for v1.** Is deferring multi-hop KG a mistake for a
   personal assistant whose recall is often relational ("who owns what", "why did X
   fail")? Or is vector + supersession enough until proven otherwise?
2. **Decay function.** Recency-weighted? Usage-count? Explicit durability flag only?
   What actually prevents both bloat and premature forgetting on a single user's data?
3. **Working-memory paging trigger.** When/how much to page in — every turn (cost) vs
   on-demand via a `recall` tool the model calls? Tool-gated recall fits our harness.
4. **Consolidation cadence & authority.** Fully autonomous nightly, or human-in-loop
   for fact promotion? Trade curation quality vs unattended value.
5. **Embedding model longevity.** nomic-768 now; re-embedding cost if we change it.
6. **Trust escalation path.** How does an untrusted memory ever *become* trusted
   without opening the injection door?

## 10. Phasing
P1 collect (ledger live, no promotion) → P2 retrieval + tool-gated `recall` over the
existing facts/vector store → P3 consolidation pass (promotion/decay) → P4 eval
harness → P5 revisit KG/temporal edges only if eval shows relational recall gaps.

## 11. Operational thresholds are DATA-DERIVED, not designed (peer round 2)
Peer round 2 narrowed the open questions to promotion criteria, consolidation
triggers, negative-memory weighting, and false-confirm/false-invalidate observability.
These read as "implementation-level" — but that does NOT license hard-coding the
values now. With an empty ledger, any threshold is the speculation §5 and both
reviews told us to avoid. So the rule for this layer:
- **Build the MECHANISM now (evidence-independent):** the state-machine schema
  (`state` + transitions), the `consolidation_log`, and the OBSERVABILITY counters —
  false_confirmations, false_invalidations, retrieval-failure-by-cause. These are
  buildable today and unblock everything else.
- **Thresholds are tunable PARAMETERS, not baked constants:** promotion/consolidation/
  penalty values live in config with conservative defaults, instrumented, and are
  *set from ledger data*, not chosen up front. The observability above is what makes
  tuning possible — that's the real answer to reviewer Q4.
- **Signal constraint (from §6b):** promotion to `confirmed` may NOT use frequency of
  mention. Repetition is exactly the poisoning vector. Confirmation requires a trusted
  signal (user, tool ground-truth, or independent multi-source agreement).
- **Negative memories** participate in retrieval as *suppressors* (down-rank a matching
  active fact / surface a "we tried this, it failed" note), never as competitors in the
  main semantic ranking — a separate retrieval path, weight tuned from data.
