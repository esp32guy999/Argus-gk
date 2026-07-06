# Session 2026-07-05 — eval suite + eval-driven select()

**Anchor:** build the keep/discard arbiter (fixed eval suite) and use it to judge
selector configs. Success: runner scores ≥2 configs live with a committed comparison.

## What shipped

- **`evals/`** — 22-task fixed suite (`tasks.yaml`), dry-run harness (`harness.py`),
  runner/scorer/comparer (`run.py`). Harness is **default-deny**: every tool is
  recorded + canned unless allowlisted read-only in `SAFE_LIVE`, so live evals can
  never mutate the homelab (verified by `tests/test_evals.py`, offline).
- **`select()` upgrade:** `SemanticSelector(always=...)` pins prompt-referenced tools
  (`lookup_memory`, `start_background_task`) into every selection. This fixes a real
  bug: the system prompt names both tools, but top-8 could silently cut them.
  Also added `rank()` so eval misses report the rank the needed tool landed at.
- `tests/test_evals.py` (offline, standalone-script convention).

## Results (gemma4-26b via llama-swap, 22 tasks, suite sha in each results file)

| config          | live success | selection hit | avg tools | avg schema tok |
|-----------------|-------------:|--------------:|----------:|---------------:|
| all (baseline)  |       22/22  |         100%  |      65   |         3,483  |
| semantic:8+core |     **22/22**|        95.5%  |      10   |       **517**  |
| semantic:8      |       21/22  |        95.5%  |       8   |           407  |

**Kept: `semantic:8+core`** — matches the all-tools baseline on success at **6.7×
less context**, and it is what production now runs (main.py's bare
`SemanticSelector(reg.all())` picks up the new defaults). Plain top-8 fails
`home-state` — exactly the selection miss `+core` covers via pinned lookup_memory.

## Gotchas (hard-won)

- **Wrapping tools for evals must use `functools.wraps`** (same as
  `registry._wrapped`). Hand-copying `__annotations__` broke type-hint resolution
  for tools whose hints reference names not imported in the wrapper's module
  (`Optional`) — and it only surfaced with `--selector all` because top-8 never
  offered the offending tool. Symptom: every task fails in 0.0s with 0 calls.
- Eval prompts must be unambiguous about the *lane*: "list what's in the movies
  folder" let the model defensibly answer via radarr instead of `list_dir`;
  calibrated the prompt to say "use the filesystem". Suite v0 was frozen after this
  (scores are only comparable at the same tasks.yaml sha — the runner records it).

## Deferred (ISSUES.md)

- MCP-lane descriptions embed poorly (`GetLiveContext` ranks 16/65 for a P1S query).
  Fix candidate: fold alias/example text into `SemanticSelector._doc()`. Measure on
  this suite before keeping.

## How to run

```bash
.venv/bin/python evals/run.py --selection-only --selector semantic:8+core   # CPU only
.venv/bin/python evals/run.py --selector all --model gemma4-26b             # live
.venv/bin/python evals/run.py --compare evals/results/A.json evals/results/B.json
```

Run the suite after ANY harness change (selector, prompts, budgets, lanes) and
compare before keeping the change — that's the point of it.

## Addendum — anti-stall batch (same day)

Driver fixes in all three run paths (`run`/`run_async`/`stream_run`, `anti_stall=True`):
announce-without-acting → one corrective retry (tail-anchored regex + zero-calls guard,
poems safe — tests/test_anti_stall.py); budget exhaustion → progress summary from the
call log instead of discarding the work. 5 multi-step chains added to the suite (27
tasks), `expect_calls` grader, per-task stall telemetry (loops/nudges/exhausted).

A/B (gemma4-26b, semantic:8+core): 26/27 both arms — no regression. Loop/exhaustion
count deltas (10→3, 2→1) are run-to-run sampling variance, NOT the fix: the nudge never
fired on gemma (0 triggers — it acts rather than announces), and the fixes only change
what happens AFTER a stall. What the batch actually buys: (1) exhaustion now returns
"Progress so far: lookup_memory → list_dir → make_dir … say continue" instead of a bare
give-up; (2) the announce guard protects the chat path + weaker models at zero cost when
untriggered; (3) stall rate is now a measured number per model.

The one persistent failure (chain-weather-list-time) is tool CONFUSION, not a stall:
gemma picks todo_get_items (read) over HassListAddItem (write), loops, wanders. Same
root cause as the open MCP-description issue — description enrichment is the fix, and
the suite will measure it. Deferred: cycle detection (A→B→A→B), plan-first scaffold.
