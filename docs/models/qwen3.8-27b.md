# qwen3.8-27b

**Identity.** Alibaba Qwen3.8-27B — dense hybrid attention+SSM (not MoE), 27B, 262K
native context, hybrid-thinking, native tool calls. Vision exists in the family but we
did not download an `mmproj`, so this seat is **text-only**. Quant: unsloth UD-Q3_K_XL
(~13.1GB on disk). GGUF arch string is `qwen35`.

**Why it's here.** Downloaded 2026-08-17; wired into llama-swap + the Argus picker
2026-08-19 so it is selectable like the rest of the lineup.

**When to use vs the others.**
- ✅ General chat and one-shot reasoning-capable work at a size that fits the 16GB
  card without MoE offload.
- ❌ Not the agentic-coding workhorse — `qwen3-next-80b` / `qwen3-coder-30b` stay
  that. Not vision until an mmproj is added.

**Serving.** → `~/llama-swap/config.yaml`, entry `qwen3.8-27b` (`-c 32768`, q4 KV,
`--reasoning off`, instruct sampling). Argus `config/models.yaml` sets `thinking: false`
(required — the chat template defaults to thinking ON at `reasoning_effort=xhigh`).

**Quirks / gotchas.**
- **Thinking defaults ON.** If `enable_thinking` is omitted the template thinks at
  xhigh and can eat the token budget. Argus must keep `thinking: false`.
- **No MTP.** The GGUF has `nextn_predict_layers = 1`; do not turn on
  `--spec-type draft-mtp` (same crash trap as Loki).
- **No mmproj.** Do not mark `vision: true` until the projector file is on disk.

**Install / uninstall.**
- Install: file already at `~/models/qwen3.8-27b/Qwen3.8-27B-UD-Q3_K_XL.gguf`.
- Uninstall: remove the `config.yaml` entry + the `models.yaml` block, delete
  `~/models/qwen3.8-27b/`.

**Last verified:** 2026-08-19. Cold load ~11s, 14659/16303 MiB at `-c 32768` (~1.1GB free).
No-think chat returned `PONG`. Native tool-call (`get_time`) emitted valid OpenAI
`tool_calls`. Live Argus `/argus/chat` path also returned `PONG`.
