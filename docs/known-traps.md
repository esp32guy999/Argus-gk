# Known Traps

Scar tissue we already paid for (Hermes) + footguns the mid-2026 research surfaced.
**Clean slate ≠ amnesia.** Read the relevant section before building each lane.

## Harvested from Hermes (infra/model bugs that recur in ANY harness)
- **64KiB stream-buffer bug** — chunk read size capped streamed responses; caused ~79% of streams to fail mid-flight. Verify the read/buffer size on the streaming path from day one.
- **Cancel path** — an in-flight generation needs an explicit cancel endpoint (`POST /api/cancel/{id}` in the old stack). Build cancellation in, not on.
- **gemma-class returns empty `content` without reasoning disabled** — gemma is a reasoning model; without `--reasoning off` it emits empty content. Set per-model.
- **`claude -p` from systemd foot-guns** (if ever shelling to a CLI): PATH missing `~/.local/bin`; a `credentials.env` `ANTHROPIC_API_KEY`/OAUTH token silently overrides Pro creds → 401; `--allowedTools` is variadic and eats the following prompt arg (use `--permission-mode bypassPermissions`).

## llama.cpp / local tool-calling
- **`--jinja` is mandatory** for tool calling (activates the GGUF-embedded chat template). Without it: `tools param requires --jinja flag`.
- **Grammar-looping on optional params** — March 2026 defect: capable ~35B models looped the same tool call forever when a tool had multiple *optional* params (grammar enforced arg order). Fixed in llama.cpp PR #20171. Mitigations: **prefer REQUIRED params**, keep llama.cpp current, rely on the watchdog loop-detection as backstop. Lesson: **reliability is plumbing-dependent, not just model-dependent.**
- **Native-first, fallback thin** — native handlers cover nearly any current model (Qwen3-Coder, GLM-4.7, gpt-oss, DeepSeek-V3.x, Nemotron). Prompted-JSON fallback only for unrecognized templates / weak models.

## LiteLLM
- `load_mcp_tools(format='openai')` is **experimental** — pin version, test the converter.
- Some "enterprise-only Prometheus" claims online are **stale** — basic Prometheus is OSS. But verify any *specific* extended label/alerting feature you depend on.

## Pydantic AI
- **"Zero-code vendor portability" is a myth** — provider quirks leak; expect per-provider config tweaks. Plan for *near*-code portability.
- Strict-compatible providers may need `openai_chat_supports_multiple_system_messages=False`.
- `OpenAIChatModel` is the current class name (formerly `OpenAIModel`).

## MCP
- **Going stateless** — 2026-07-28 RC drops the initialize handshake + `Mcp-Session-Id` header, moves capabilities into per-request `_meta`, adds `server/discover`. It's a **release candidate** — design stateless-ready but **don't hard-code against it**.
- **Streamable HTTP** is the committed remote transport ("no more official transports this cycle"); keep **stdio for local** processes only.
- `.well-known` "Server Cards" discovery is **roadmap/planned, not shipped**.

## Observability
- OTel **GenAI semantic conventions are "Development" maturity** — attribute names can change without a major bump, and the spec content relocated repos (`open-telemetry/semantic-conventions-genai`). **Pin versions**; treat OTel spans as additive over a stable Prometheus base.
- The fixed-enum of `gen_ai.operation.name` values is **not** a hard MUST (claim refuted in research) — don't build hard validation around it.

## Tool-count / context
- Models — especially local — **degrade badly past ~20–40 tools** in context. **Curate, never dump.** This is acute for OpenAPI import (one spec can be 100+ endpoints).

## Architecture discipline
- **One source of truth per fact** — the provider owns tool existence; registry mirrors; any index/cache is derived and never authoritative. Two truths = drift = the bug that rots the system.
