# Argus — walking skeleton

Personal AI assistant / agent harness. See [SPEC.md](SPEC.md) for the design and
[docs/known-traps.md](docs/known-traps.md) for footguns.

## What works in this skeleton (build-order step 1)
The end-to-end spine: **LiteLLM (model layer) → Pydantic AI loop → registry + `select()` seam → native tools → Prometheus sensors.**
Two native tools (`get_time`, `calc`) exercise the tool contract, teaching-errors,
metrics, the `select()` seam, and the turn budget. The n8n / MCP / OpenAPI lanes
are the next steps.

## Run it
```bash
# 1. deps
pip install -r requirements.txt
pip install 'litellm[proxy]>=1.0'

# 2. model layer (terminal A) — OpenAI-compat on :4000, metrics on /metrics
litellm --config config/litellm.config.yaml
#   edit config/litellm.config.yaml to point at your model (default: llama-swap on anvil)

# 3. the harness (terminal B)
python -m argus.main "what time is it, and what is 3*(4+1)?"
#   harness metrics: curl localhost:9101/metrics
#   model metrics:   curl localhost:4000/metrics
```

## Resuming without losing work
Everything is on disk and in git. If a session runs out, `git log` shows the last
commit — nothing is lost (unlike a research run). Build proceeds in SPEC.md order,
committed per step.

## Version notes (if it fails to import/run)
Written against `pydantic-ai>=1.x` but NOT yet executed in this environment. If the
API drifted, the only likely edits are isolated to `argus/loop.py`:
- `OpenAIChatModel` / `OpenAIProvider` import paths
- `result.output` (older versions: `result.data`)
- `UsageLimits(request_limit=...)` keyword name
