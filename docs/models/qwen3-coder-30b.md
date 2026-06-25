# qwen3-coder-30b — UNDER EVAL (downloading 2026-06-25)

**Status:** bake-off candidate, not yet wired. Downloading IQ4_XS (16.4GB, single file).
The question it answers: do you even *need* the 80B-class coder, or does this fast/light one
get most of the way with VRAM to spare?

**Identity.** Qwen3-Coder-30B-A3B-Instruct — **30B total / 3B active** MoE (256 experts),
non-reasoning instruct, Apache 2.0. Released 2025-07; still a strong agentic coder.

**Why it's a candidate.** SWE-bench Verified **~64** (vs Coder-Next's 71.3). The trade: at
16.4GB it fits **mostly on the GPU** with light offload, so it runs **fast** and leaves VRAM
headroom — unlike the 80B's full CPU-offload + ~163s cold-load. If the ~7-point SWE gap
doesn't matter for daily coding, this is the pragmatic pick.

**Serving (when wired).** → `~/llama-swap/config.yaml` (entry pre-staged, commented). Single
file: `Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf`. Start `-ncmoe 12 -c 16384` and tune `-ncmoe`
*down* for more speed if it fits (16.4GB on a 16GB card can't be fully GPU-resident with KV +
context, so some offload is needed). `qwen3moe` arch — supported on build 9425.

**Quirks / open questions.**
- The whole point is speed + VRAM headroom; confirm decode tok/s and the lowest `-ncmoe` that
  loads with a usable context.
- Lower ceiling than Coder-Next on hard multi-file tasks — the bake-off says how much.

**Install.** `hf download unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF --include "*IQ4_XS*"
--local-dir ~/models/qwen3-coder-30b`. Then uncomment the config entry.

**Bake-off.** Run `/tmp/bakeoff/battery2.py qwen3-coder-30b` vs the 80B baseline + Coder-Next.

**Last verified:** — (pending download + bake-off)
