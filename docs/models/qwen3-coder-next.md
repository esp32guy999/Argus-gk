# qwen3-coder-next — UNDER EVAL (downloading 2026-06-25)

**Status:** bake-off candidate, not yet wired. Downloading IQ4_XS (~42.7GB, sharded).
The headline question: does the coder-tuned 80B **replace `qwen3-next-80b`** as the local coder?

**Identity.** Qwen3-Coder-Next — coder-tuned MoE built on the Qwen3-Next-80B-A3B base.
**80B total / 3B active** (512 experts, 10 + 1 shared), hybrid attention, **non-reasoning
instruct** (no `<think>` blocks), **262K** native context. So it runs *exactly* like the
current 80B (3B active via `-ncmoe` offload) — same footprint, same speed class.

**Why it's a candidate.** SWE-bench **Verified 71.3** / Pro 44.3 (OpenHands) — the top open
coder that fits the box. The current `qwen3-next-80b` is a *general* instruct model; this is
the same base post-trained for agentic coding. Non-reasoning = no reasoning-runaway tax, so
the speed comparison vs the 80B is clean.

**Serving (when wired).** → `~/llama-swap/config.yaml` (entry pre-staged, commented). Mirror
the 80B: `-ncmoe 33 -c 8192`. IQ4_XS is **sharded** — point `-m` at the first shard
(`…-IQ4_XS-00001-of-0000N.gguf`). Verify it loads on build 9425 (same qwen3-next arch as the
80B, so arch support is expected).

**Quirks / open questions.**
- 262K context *capability*, but VRAM caps the served `-c` (the 80B runs at 8192). Whether we
  can lift that meaningfully on this box is TBD.
- Same ~163s cold-load pain as the 80B (40GB+, heavy offload).

**Install.** `hf download unsloth/Qwen3-Coder-Next-GGUF --include "*IQ4_XS*" --local-dir
~/models/qwen3-coder-next`. Then uncomment the config entry + fix the `-m` shard path.

**Bake-off.** Run `/tmp/bakeoff/battery2.py qwen3-coder-next` vs the 80B baseline + the 30B.
If it beats the 80B on the real-coding battery at similar speed, it's a drop-in replacement.

**Last verified:** — (pending download + bake-off)
