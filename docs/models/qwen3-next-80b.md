# qwen3-next-80b

**Identity.** Qwen3-Next 80B-A3B — 80B total / **3B active** MoE, **instruct-only** (no
reasoning mode, no thinking toggle). The current local agentic-coding workhorse. ~40GB on
disk (IQ4-class).

**Why it's here.** Won the head-to-head on real coding: on the patch task that separated
the field it shipped a **correct diff in ~2s / 2.4K tokens** — no reasoning overhead. GLM
hung, Qwen3.6 needed ~113s to match it. Terse instruct output is a feature for agentic loops.

**When to use vs the others.**
- ✅ Tool-heavy, multi-step / agentic coding where speed and token economy matter.
- ❌ Long conversations or context-heavy tasks — **it's capped at 8K context** (see quirks).
  Reach for `qwen3.6-35b-a3b` there.

**Serving.** → `~/llama-swap/config.yaml`, entry `qwen3-next-80b` (`-ncmoe 33`, heavy expert
offload to fit the 16GB card). Argus default local model (`ARGUS_DEFAULT_MODEL`).

**Quirks / gotchas.**
- **8192-token context ceiling.** Served at `-c 8192` (VRAM-bound). Argus enforces it via
  `CONTEXT_WINDOW = {"qwen3-next-80b": 8192}` + history budgeting, or llama.cpp truncates the
  prompt from the front (dropping the system prompt). This is the model's biggest limitation.
- **Brutal cold load (~163s)** — it's 40GB with `-ncmoe 33`. Hence `WARM_ON_SELECT` + the
  phone-buzz warm path in the UI. Keep it unloaded when on claude-code to avoid RAM pressure.
- **Instruct-only:** no `reasoning_content`, no `enable_thinking` (it's not in `CHAT_THINKING`
  and wouldn't use the kwarg anyway).

**Install / uninstall.** Already the default; lives at `~/models/qwen3-next-80b-a3b/`. To
retire it, swap `ARGUS_DEFAULT_MODEL` first, then remove the config entry + `WARM_ON_SELECT`
/ `CONTEXT_WINDOW` references in `ui/server.py`.

**Possible upgrade:** **Qwen3-Coder-Next** (same 80B-A3B family, coder-tuned, non-reasoning,
SWE-Verified ~70, ~256K context) is the drop-in worth bake-off testing — same footprint,
better coder, and it lifts the context ceiling. Not yet evaluated.

**Bake-off (2026-06-24).** Round 1: 19/19. Round 2 (real patch): correct + fastest. The coder.

**Last verified:** 2026-06-25 (in active use as Argus default).
