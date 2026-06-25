# qwen3.6-35b-a3b

**Identity.** Alibaba Qwen3.6, hybrid attention+SSM MoE — 35B total / **3B active**
(256 experts, 8 used), 262K native context, hybrid-*thinking*. GGUF arch string is
`qwen35moe` (supported on our llama.cpp build 9425 — this was the real risk and it cleared).
Quant: unsloth UD-Q4_K_XL (~22GB on disk).

**Why it's here.** Replaced GLM-4.7-Flash on 2026-06-24 as the **fast no-think chat
model**. It strictly dominates GLM: same ~0.1–0.5s no-think chat *plus* a genuinely
working reasoning-coder mode. See [glm-4.7-flash.md](glm-4.7-flash.md) for that comparison.

**When to use vs the others.**
- ✅ Chat, light one-shot coding, anything needing **big context** (the 80B is capped at
  8K — see [qwen3-next-80b.md](qwen3-next-80b.md)), or when you want a reasoning pass and
  can afford the latency.
- ❌ **Not** the agentic-coding workhorse — the 80B is faster/cheaper there (below).

**Serving.** → `~/llama-swap/config.yaml`, entry `qwen3.6-35b-a3b` (currently `-ncmoe 20
-c 16384`, q4 KV → ~13GB on the 16GB card, ~3GB free). Decode ~86 tok/s at that offload;
lower `-ncmoe` for more speed if VRAM allows.

**Quirks / gotchas (the important part).**
- **It runs away without token headroom.** With thinking on it can emit 25–32K chars of
  reasoning before answering; under a tight `max_tokens` it hits the cap and emits
  **nothing**. Give it room (or thinking off).
- **Thinking ON = correct but ~50× slower.** On a real code patch it produced a correct
  diff in ~113s / 9.4K tokens vs the 80B's ~2s / 2.4K. Reasoning helps correctness on
  hard/multi-step tasks but is brutal on latency/budget.
- **No-think coding:** excellent for self-contained tasks (wrote 4/4 algorithms perfectly,
  ~2s each), but **breaks on multi-step planning** (confabulated a nonexistent method on a
  thread-a-param-through-3-functions task). For real multi-file changes: thinking on, or 80B.
- **Toggle:** disable reasoning per-request via `chat_template_kwargs.enable_thinking=false`.
  Wired in Argus as `loop.py`'s `enable_thinking` param, gated by `CHAT_THINKING` in
  `ui/server.py` (chat path runs it no-think → 0.1s). `reasoning_effort` is a **no-op** for
  this model; `--reasoning-format none` is the WRONG knob (inlines `<think>` into content).

**Install / uninstall.**
- Install: `hf download unsloth/Qwen3.6-35B-A3B-GGUF Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
  --local-dir ~/models/qwen3.6-35b-a3b`, then the config entry above. Loads on build 9425.
- Uninstall: remove the `config.yaml` entry, drop from `CHAT_THINKING`/UI accent, delete
  `~/models/qwen3.6-35b-a3b/`.

**Bake-off (2026-06-24).** Round 1 (easy algos): tied 19/19 with GLM and 80B. Round 2
(real `loop.py` patch): correct (think-on) but slow; GLM failed; 80B won on speed.

**Last verified:** 2026-06-25 (loads, chat, native tool-calls w/ valid JSON, enable_thinking
toggle all working via llama-swap).
