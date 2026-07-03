# Model profile — Ornith 35B AEON Uncensored (2026-07-03)

`vcruz305/Ornith-1.0-35B-AEON-Ultimate-Uncensored-GGUF`, Q4_K_M (20GB) at
`anvil:~/models/ornith-35b-uncensored/`. Three llama-swap arms: `ornith-35b-uncensored`
(baseline), `-ngram` (n-gram self-spec), `-mtp` (MTP-head draft, separate 21GB GGUF).

## What it is

- **Qwen3.5-MoE base: ~35B total, ~9B active** (4/64 experts) — with `-ncmoe 20` expert
  offload it runs **85 tok/s gen / 172 tok/s prompt** on the 16GB card. Fastest capable
  model in the stable.
- **Reasoning model** (ChatML + DeepSeek-style think tags); imatrix calibrated on
  coding/debugging/system-design/reasoning prompts. Uncensored finetune (AEON-7).
- Served at 16k ctx, q4_0 KV, Qwen house samplers (temp .6 / top-p .95 / top-k 20).

## Probe results (all 2026-07-03, via llama-swap `--jinja`)

| Probe | Result |
|---|---|
| Native tool call | ✅ clean `tool_calls` JSON, correct args, `finish_reason: tool_calls` |
| Tool-result round-trip | ✅ correct synthesis of returned JSON |
| SEARCH/REPLACE edit | ✅ byte-perfect block, correct fix, zero extra text (thinking off) |
| Unbounded thinking | ❌ hard prompt burned a 900-token budget entirely on reasoning, EMPTY content |
| `enable_thinking:false` | ✅ honored — reasoning fully suppressed, direct answer |

## Harness profile (what was configured where)

- **Lanes**: full toolset + **cleared for `code_edit`** (`loop.LANE_MODEL_GATES`,
  substring `ornith-35b` covers all three arms).
- **Thinking OFF on chat path** (`ui/server.py CHAT_THINKING`) — same failure mode and
  same fix as qwen3.6. Flip per-model there if a use case wants budgeted reasoning.
- **CONTEXT_WINDOW 16384** (all arms) so history budgeting matches the served `-c`.
- **WARM_ON_SELECT** (all arms) — 20GB cold-load; warming + ready-buzz like the 80B.
- **Persona overlay**: `soul.d/ornith-35b.md` via the new soul.d mechanism — unfiltered
  register, decisive answers, knows to punt huge-context jobs to 80B/claude-code.
- **UI accent**: crimson (all arms).

## Division of labor (the point of the profile)

| Job | Model |
|---|---|
| Daily driver: chat, tools, homelab actions, blunt opinions, unfiltered anything | **ornith-35b** (fast + full lanes) |
| Pattern-following code drafts, surgical edits | ornith or qwen3-coder-30b (both code_edit-cleared; ornith is faster, coder-30b won the bake-off — compare) |
| Big-context or deep multi-step reasoning | qwen3-next-80b |
| Vision, native-tool heavy work, anything touching the wider world | claude-code |

## Not done / watchlist

- Model card recommends `--repeat-penalty 1.1`; left OFF to match Qwen house samplers
  and avoid corrupting tool-call JSON. If long creative sessions get loopy, add it to
  the llama-swap entries.
- Card claims 131k ctx capable; served at 16k for VRAM. If a use case needs more, try
  32k and watch VRAM before committing.
- MTP arm (`-mtp`, claimed 1.8x speedup) unbenchmarked vs baseline in agent use — the
  A/B arms exist for exactly that; `enable_thinking`/tool behavior assumed identical
  but only baseline was probed.
