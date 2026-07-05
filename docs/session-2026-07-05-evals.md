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
