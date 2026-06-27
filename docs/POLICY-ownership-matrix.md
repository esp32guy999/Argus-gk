# Policy: Ownership Matrix + Non-Collapsible Gates

Stolen from SAW (safe-agentic-workflow): every role declares **what it owns, what it must
NOT do, and its exit state** — and certain quality gates are **never collapsible**, even
when the loop runs autonomously. This pins down the doer/judge/floor/human boundaries we
keep referring to verbally.

## Ownership matrix

| Role | Owns | Must NOT | Exit state |
|---|---|---|---|
| **qwen3-coder-30b** (DOER) | Writing/editing source via the `code_edit` lane; agentic tool use | Judge/audit/evaluate; decide its own work is correct; act as critic | An applied edit — *verified by tests/Verify, never by itself* |
| **qwen3-next-80b** (AUDITOR) | Critique/evaluation (Reflections Critic, code Critic) | Write or merge code; be trusted for its confidence scores; have findings auto-actioned | **Grounded, cited candidate findings** — awaiting Verify, not truth |
| **`loop_guard`** (the FLOOR) | Deterministic safety: iteration cap, token budget, evidence-gate, Stop-the-Line, audit | Be collapsed/disabled while `ARGUS_LOOP_ENABLED=1` | allow/deny + an audit line |
| **Verify stage** (to build) | Adversarial check that a grounded finding is *real*, not just located | Be skipped/collapsed — see non-collapsible gates | real / refuted verdict |
| **the agent loop** (doing a turn) | Reasoning + tool calls within the turn | **Invent requirements** (Stop-the-Line); fabricate success; auto-act on irreversible/high-blast-radius work | A result + *deferred items surfaced* |
| **the human (Shane)** | Approving proposals; final merge; irreversible/outward-facing calls; **defining `success_condition`s** | — (must not be bypassed for the above) | approve / reject |
| **tool lanes** (`code_edit`, `media_fs`) | Scoped mutations under a path allowlist, reversibly (backup/trash) | Touch outside roots; hard-delete by default | applied + audited + **reversible** |

The spine: **the role that DOES is never the role that JUDGES, and neither is the role that
APPROVES.** Doer (30b) → Judge (80b) → Verify → Human. A good coder is not a good critic;
a confident critic is not ground truth; ground truth is not a human decision.

## Non-collapsible gates (never skipped, even autonomous)

SAW's rule: some roles can be collapsed for speed (routing, cadence, model choice), but
**quality/safety gates are never collapsible.** Argus's non-collapsible set:

1. **Stop-the-Line** — an agent job with no `success_condition` is not actionable. The loop
   cannot invent what "done" means. *(Enforced in `loop_guard.actionable_jobs`.)*
2. **Evidence-gate** — a finding with no real citation is dropped before it can act.
   *(Enforced in the Critic scripts' mechanical floor.)*
3. **Verify** — grounded ≠ correct, so an independent check precedes any action on a finding.
   *(To build; mark it non-collapsible when it lands.)*
4. **Iteration cap + master switch** — the loop cannot run unbounded; `loop_guard` caps
   consecutive auto-continues and the whole thing is OFF unless explicitly enabled.
5. **Human approval** for irreversible / high-blast-radius / outward-facing actions
   (decide-and-notify vs stop-and-ask, per `docs/POLICY-scope-control.md`).

Collapsible (streamline freely): routing, reflection cadence, model selection, the article
intake flow. **Gates: never.**
