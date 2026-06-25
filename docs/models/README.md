# Model wiki

One memo per model that has **non-obvious quirks, a niche, or a runbook** — the
hard-won stuff that config params alone don't capture and that doesn't survive a
fresh chat thread otherwise.

## Rules to keep these useful (read before editing)
- **`~/llama-swap/config.yaml` is the single source of truth for serving params**
  (`-ncmoe`, `-c`, sampling, ttl). These memos describe *why/quirks/runbook* and
  **link** to the config — never restate launch flags here, or they'll drift.
- Stamp a **Last verified** date. A wrong memo is worse than none.
- Not every model earns one. Stock, self-explanatory models (gemma, gpt-oss, the
  small ones) are listed below without a memo until they develop a quirk worth recording.

## Template
> **Identity & why it's here** · **When to use vs the others** · **Serving** (→ config link)
> · **Quirks / gotchas** · **Install / uninstall runbook** · **Bake-off results** · **Last verified**

## The lineup (anvil, llama-swap :9090)
| Model | Role | Memo |
|---|---|---|
| `qwen3-next-80b` | **Agentic coding workhorse** (instruct, fast, terse) | [memo](qwen3-next-80b.md) |
| `qwen3.6-35b-a3b` | **Fast no-think chat** + flexible reasoner (replaced GLM) | [memo](qwen3.6-35b-a3b.md) |
| `qwen3-coder-next` | *under eval* — coder-tuned 80B (SWE 71); may replace the 80B | [memo](qwen3-coder-next.md) |
| `qwen3-coder-30b` | *under eval* — fast/light coder (SWE ~64), fits mostly on GPU | [memo](qwen3-coder-30b.md) |
| `gemma4-26b` | Default general chat (reasoning off) | — |
| `gemma4-12b` | Smaller general chat | — |
| `gpt-oss-20b` | General/utility | — |
| `bonsai-8b` | Small/fast | — |
| `lfm2.5-8b` | Small/fast | — |
| `z-engineer` | Media model (chat selector — see docs/ISSUES.md) | — |
| `claude-code` | Cloud heavy-hitter (own path, not via loop.py) | — |
| ~~`glm-4.7-flash`~~ | **Removed 2026-06-24** | [how to bring back](glm-4.7-flash.md) |
