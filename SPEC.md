# Argus — Personal AI Assistant / Agent Harness

> Greenfield. Replaces the retired Hermes chat brain. **Inherit the lessons, not the code.**
> Name `argus` (hundred-eyed all-seer — fits "everything gets a sensor"); placeholder, rename freely.
> Last updated: 2026-06-20.

## Principles (the non-negotiables)

1. **Model-agnostic** — any OpenAI-compatible endpoint, hosted or local (llama.cpp/llama-swap).
2. **Minimum new code** — adopt proven infra; write only the glue that is genuinely ours.
3. **Everything gets a sensor** — every tool, model call, and control event emits Prometheus metrics.
4. **Clean siloing** — each tool source is an isolated provider behind one uniform contract; `git rm` a provider and nothing else notices.
5. **Seam, not implementation** — build the interface, keep the first implementation stupid, upgrade behind the seam. (This is how we avoid rebuilding the poisoned pile.)
6. **One source of truth per fact** — providers own whether a tool exists; everything else mirrors. Never a DB-of-tools authoritative over the provider.

## Architecture (one line)

The **Argus harness** — *our orchestrator: registry, `select()`, watchdog, metrics, session/storage* — drives a **Pydantic AI** loop *engine* over **LiteLLM** (model layer + MCP gateway), pulling tools from **4 provider lanes**, persisting to **SQLite + sqlite-vec**, instrumented **Prometheus-first**.

> **What's ours vs. borrowed:** LiteLLM (below) and Pydantic AI (the loop engine) are borrowed, proven pieces. The **harness** — everything that makes Argus *Argus* — is our code. Pydantic AI is *driven by* the harness, not the harness itself.

```
┌─ ARGUS HARNESS (our code — the product) ───────────────────────────┐
│                                                                    │
│  user ─▶ session ─▶ loop driver ──drives──▶ [ Pydantic AI engine ] │
│                        │                                           │
│                        ├─ registry.select(context) -> tool subset  │
│                        │     ▲ assembled from 4 provider lanes      │
│                        ├─ watchdog (budget / loop / no-progress)    │
│                        ├─ metrics (prometheus_client)               │
│                        └─ storage (SQLite+vec: audit/history/ledger)│
└────────────────────────────────┬───────────────────────────────────┘
                                  │ chat + selected tools
                                  ▼
                    LiteLLM  ──/metrics──▶ Prometheus
                     │  │
                     │  └─ MCP gateway ─▶ real MCP servers ─┐ (2) MCP lane
                     ▼                                      │
            any OpenAI-compat model        (1) n8n webhook shim
            (hosted / llama.cpp --jinja)   (3) OpenAPI curated import
                                           (4) native python lane
```

---

## Decisions (ADR — decision · rationale · rejected alternative)

### Harness — **Argus orchestrator (our code — the product)**
- The harness is the application we own. It is **not** Pydantic AI; it *drives* Pydantic AI.
- Responsibilities (each a clearly-owned module, so the loop never becomes a god-object):
  - **session** — conversation state, entry point / API surface.
  - **loop driver** — wraps the Pydantic AI engine; supplies the per-turn toolset, routes dispatch results + `ModelRetry`, writes the audit log.
  - **registry** — assembles + namespaces tools from the 4 provider lanes (providers = source of truth).
  - **selector** — the `select(context) -> subset` seam.
  - **watchdog** — turn budget / loop / no-progress; emits metrics.
  - **metrics** — `prometheus_client` exporter.
  - **storage** — SQLite+sqlite-vec data-access layer.
- **Discipline:** these stay separate modules with explicit interfaces. The loop driver orchestrates them; it does not absorb them. (This is the structural guard against re-poisoning — "keep it thin" enforced by module boundaries, not willpower.)

### Model layer — **LiteLLM**
- Model-agnostic OpenAI-compatible gateway; **also** the MCP gateway for real MCP servers (transport, namespacing by `{server}{sep}{tool}`, auth/access-control by key/team).
- Prometheus `/metrics` is **OSS-tier, not enterprise-gated** (blogs saying otherwise are stale). Scrape it — it delivers most of the model-layer sensors for free.
- **Footgun:** `load_mcp_tools(format='openai')` is flagged *experimental* — pin the version, test the converter, don't assume stability.

### Agent loop — **Pydantic AI**
- `OpenAIChatModel` + `LiteLLMProvider(base_url=<litellm>)`.
- Tools supplied **per-turn** as a dynamic toolset built from `registry.select(context)`.
- Dispatch failures raised as **`ModelRetry`** → model self-corrects in-context. This *is* our "teaching error message" mechanism.
- **Validate before committing** (two integration points): (1) per-turn dynamic toolset injection, (2) `ModelRetry`-as-teaching-error path.
- **Reality check:** "swap the model object, zero other code changes" is a *myth* — provider quirks leak. Model-agnostic is **near-code, not zero-code**. Strict providers may need `openai_chat_supports_multiple_system_messages=False`.
- *Rejected:* hand-rolled loop (re-poisoning risk; "I'll keep it thin" is the promise that built the last pile) and LangGraph (heavier cage to fight).

### Tool registry & contract
- **In-memory**, assembled at startup from the 4 providers, refreshed on change. **Providers are the source of truth.**
- Per-tool JSON contract:
  - `name` · namespaced id `provider.tool`
  - `description` — concise: what + when + the one gotcha
  - `tags: []` — multi-label; drives `select()`
  - `schema` — typed, per-field descriptions, **enums**; does the heavy teaching
  - `example` — exactly one sample call (cheap insurance for weak models)
  - `dispatch` — errors written **to be read by the model**
- **Prefer REQUIRED params over optional** in schemas (llama.cpp grammar-looping lesson — see traps).

### Tool sources (4 siloed lanes)
1. **n8n → webhook-as-tool shim.** One workflow = one callable, full contract control. *Rejected:* n8n MCP Server Trigger — exposes inner tool nodes (wrong granularity) and adds indirection.
2. **MCP servers (real ones only)** — HA, Gmail, Calendar, Drive, filesystem, etc. Consumed via **LiteLLM's MCP gateway** for transport/namespacing/auth; tool list ingested into **our** registry so `select()` keeps **per-turn** control (LiteLLM's own filtering is static per-key, not per-message). **Stateless-ready**, Streamable HTTP for remote / stdio for local. **Do not hard-code against the 2026-07-28 RC** (handshake/session-header removal still a release candidate).
3. **OpenAPI import → thin custom importer.** Load spec → **operationId allowlist (curate, never dump)** → map to contract. Fallback lib: `vblagoje/openapi-llm` if the custom one bloats. Curation mandatory (Radarr alone is 100+ endpoints).
4. **Native python lane** — trivial/latency-critical only (time, math, single HA toggle). Keep small. **No generic shell lane** (that's how the bash-skill pile started).

### Tool selection — `select(context) -> subset` seam
- **v1:** tag-filter, in-memory. No DB.
- **v2 (deferred):** semantic/vector retrieval behind the *same* seam, as a derived cache off the registry. Only when tool count actually hurts (~40+ tools). Optional `find_tools()` meta-tool then.
- The seam exists from day one; the first implementation stays dumb.

### Control — watchdog
- **Turn budget** (hard max iterations/task).
- **Repeated-call detection** (same tool+args twice → intervene via `ModelRetry` nudge).
- **No-progress detection** (N turns, no successful new result → graceful give-up).
- All signals **emit Prometheus metrics** — the watchdog *is* a sensor.
- **No armed/unarmed per-tool state machine** (turn boundary already gives atomicity).
- Goal-per-call is a **watchdog signal, never a gate** (a confused model must not be able to deadlock itself).
- **Async in-flight tracking** (only when long-running tools are added): a separate **invocation ledger** in SQLite — distinct from whether a tool is exposed to the model.

### Storage — **SQLite + sqlite-vec**
- Holds: tool-call **audit log** (provider, tool, args, result, latency, model's stated goal), **conversation history**, **async invocation ledger**, **memory/embeddings**.
- `sqlite-vec` covers future vector needs (incl. v2 tool retrieval) at zero ops.
- Clean **data-access seam** so a later Postgres+pgvector swap isn't a rewrite.
- *Rejected (for now):* Postgres+pgvector — only earns its place under real write concurrency; the one future vector use (tool retrieval) is tiny and deferred.

### Observability — **Prometheus-first**
- **LiteLLM `/metrics`** = model-layer sensors (total/LLM/overhead latency, TTFT, per-provider success/failure, rate-limit headroom; labeled by provider/model/api_base/hashed-key).
- **Harness `prometheus_client`** metrics (the "every tool gets a sensor" set):
  - `argus_tool_calls_total{provider,tool,outcome}`
  - `argus_tool_latency_seconds{provider,tool}` (histogram)
  - `argus_tool_errors_total{provider,tool,error_type}`
  - `argus_tool_retry_total{tool}`
  - `argus_agent_turns_total{outcome}`
  - `argus_agent_loop_detected_total`
  - `argus_agent_no_progress_total`
  - `argus_agent_task_duration_seconds` (histogram)
  - `argus_tools_selected_per_turn` (histogram)
- **OTel GenAI spans** (`execute_tool`, `gen_ai.tool.*`) in parallel — but **pin the convention version**; it's "Development" maturity and can break without a major bump. Prometheus is the stable substrate; OTel is additive.

### Local model serving
- llama.cpp / llama-swap, launch with **`--jinja`** (required for tool calls; without it: `tools param requires --jinja flag`).
- **Keep llama.cpp current** — grammar/constraint bugs are point-in-time (see traps).
- **Native-first** tool calling; thin **prompted-JSON fallback** only for unrecognized templates / known-weak models.

---

## Known traps
See [`docs/known-traps.md`](docs/known-traps.md) — harvested Hermes scar tissue + research footguns. Read before building each lane.

## Open / deferred
- **Deploy host** — candidate: harness on **nyx** (where the chat brain lived), models on **anvil**. Decide before scaffolding services.
- **Project name** — `argus` placeholder.
- **OpenAPI lib** — custom vs `openapi-llm`, finalize after a small spike.
- **Semantic tool retrieval** — v2, behind the seam.

## Build order (walking skeleton first)
1. **Skeleton end-to-end:** LiteLLM up → Pydantic AI loop hits it → one native tool → Prometheus scrape green. Prove the spine before adding lanes.
2. **Registry + contract + `select()` tag-filter + watchdog + metrics.**
3. **n8n webhook lane** — port the proven shim into a provider.
4. **MCP lane** via LiteLLM gateway — start with Home Assistant.
5. **OpenAPI lane** — curated, Radarr first.
6. **Hardening** — teaching errors, fallback path, audit log, OTel spans.
