# glm-4.7-flash — REMOVED 2026-06-24

**Status:** removed from the lineup and deleted from disk (reclaimed 17GB). This memo is the
**bring-back runbook** + the record of why it left, so the decision isn't re-litigated.

**Why removed.** `qwen3.6-35b-a3b` **strictly dominates it**: same ~0.5s no-think chat niche,
*plus* a working reasoning-coder mode GLM lacked. GLM had no remaining advantage.

**What it was.** Zhipu GLM-4.7-Flash — 30B MoE ~3.6B active, reasoning, agentic. unsloth
UD-Q4_K_XL (~17GB). Native tool-calls verified on build 9425 (`Chat format: GLM 4.5`,
template carries `<arg_key>`/`<arg_value>` tags; #19009 corruption regression did **not**
manifest here). So it *worked* — it just wasn't better than Qwen3.6 at anything.

**Bake-off findings (2026-06-24).** Round 1 (easy algos): tied 19/19. Round 2 (real `loop.py`
patch): **failed** — thinking-on ran away (25K chars reasoning, hit token cap, emitted
nothing); thinking-off produced a broken diff. The 80B won; GLM was the dud.

**Bring-back runbook (if ever needed):**
1. `hf download unsloth/GLM-4.7-Flash-GGUF GLM-4.7-Flash-UD-Q4_K_XL.gguf --local-dir
   ~/models/glm-4.7-flash`  *(verify exact repo/filename on HF first)*
2. Add to `~/llama-swap/config.yaml`:
   ```yaml
   "glm-4.7-flash":
     cmd: |
       ${server}
       -m /home/shane/models/glm-4.7-flash/GLM-4.7-Flash-UD-Q4_K_XL.gguf
       --cache-type-k q4_0 --cache-type-v q4_0
       -ncmoe 16 -c 16384
       --temp 0.7 --top-p 1.0 --min-p 0.01
     ttl: 600
   ```
   (~12.9GB used, ~3GB free at `-ncmoe 16`.)
3. For fast no-think chat, add `"glm-4.7-flash": False` to `CHAT_THINKING` in `ui/server.py`
   (same `chat_template_kwargs.enable_thinking` mechanism as Qwen3.6).

**Last verified:** 2026-06-24 (the day it was tested and removed).
