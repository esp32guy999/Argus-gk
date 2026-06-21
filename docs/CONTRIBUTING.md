# Working on Argus — the dev loop

Read this first (a fresh `claude -p` asked for exactly these two things: *how to add a
lane* and *how to run tests*). For the *why* / architecture, see `SPEC.md`. For what
changed recently, read the latest `docs/session-*.md` + `git log --oneline -25`.

Argus is a thin orchestrator (registry · `select()` seam · watchdog · Prometheus ·
SQLite/vec storage) that **drives** a Pydantic AI loop over an OpenAI-compatible model,
pulling tools from provider lanes. The harness is what we own; the loop engine is borrowed.

---

## Running the tests
**There is no pytest.** Each `tests/test_*.py` is a standalone script (sys.path shim +
`if __name__ == "__main__": sys.exit(main())`) that prints `PASS:` lines and exits 0/1.

```bash
cd ~/argus
.venv/bin/python tests/test_storage.py                 # one
for t in tests/test_*.py; do printf "%-26s " "$(basename $t)"; \
  PYTHONPATH=. .venv/bin/python "$t" >/dev/null 2>&1 && echo PASS || echo "FAIL ($?)"; done
```
Tests are **offline** — they stub external HTTP (fake server / monkeypatched embed) so
the suite runs without the homelab. `test_server.py` is the exception (needs the live
80B at :8099) and is the one allowed red.

## Adding a tool lane
A lane is one module exposing `tools() -> list[Tool]`. `Tool` is `from ..registry import Tool`.

```python
# argus/tools/mylane.py
from ..registry import Tool
from pydantic_ai.exceptions import ModelRetry   # raise on failure → TEACHES the model (never crash)

def mything(query: str) -> dict:
    """Docstring = the model-facing description. Prefer REQUIRED params (grammar-loop lesson)."""
    ...

def tools(manifest_path: str = "config/mylane.yaml") -> list[Tool]:
    return [Tool(name="mything", description="…", tags=["…"],
                 func=mything, provider="mylane",
                 example={"query": "…"}, schema=None)]   # schema only for external-API lanes
```

Wire it in `argus/main.py` `build_registry()`:
- **always-on** (no secrets/config): `reg.add_provider(mylane.tools())` (like native/web/notes/weather).
- **config-gated** (needs a manifest): append `("mylane", "config/mylane.yaml", mylane.tools)` to `lanes`, guarded by `os.path.exists` — a broken/missing manifest warns and is skipped, never crashes boot.

Add `tests/test_mylane.py` (stub the external calls). Done — the registry auto-instruments
dispatch (latency/count/error metrics) and `select()` surfaces it semantically.

## Conventions / rules (the stuff not in the code)
- **Seam, not implementation. Minimum new code** — borrow LiteLLM + Pydantic AI; write only glue.
- **Keep the loop driver thin** — registry/selector/watchdog/storage stay separate modules; the
  driver must never absorb them (structural guard against re-poisoning).
- **`ModelRetry` for recoverable tool failures**, not exceptions — the message teaches the model.
- **Everything emits Prometheus**; instrument once at the boundary (registry/metrics), not per-function.
- **Providers are the single source of truth** for tool existence; registry mirrors. One truth per fact.
- **No Hermes/dead-stack code** — inherit lessons, not code; write fresh. (Forge fork is fine.)
- **Build loop:** local Qwen3-80B drafts pattern-following lanes → a contract test validates →
  Claude reviews. Each lane ships with its `tests/test_*.py`.
- **Secrets** live only in gitignored `config/*.yaml` / the env (`~/.config/secrets/credentials.env`);
  never in committed code or docs. Scan before pushing. See `~/secrets-inventory.md`.
- **Ops changes (services/secrets): wire → verify → remove**, never remove-then-wire; back up first.
- **Commits:** per-feature, end the message with
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Push to the private GitHub remote
  after substantive work.

## Doc/test enforcement (not just a request)
A written "remember to update docs" rule rots. These are mechanical:
- **`tests/test_docs.py`** (in the suite) fails if any `argus/tools/` lane has no test, or
  an onboarding doc is missing. Green-is-the-bar enforces coverage.
- **`hooks/pre-commit`** (version-controlled) **blocks** a commit that breaks that invariant
  and **nudges** when you change `argus/` code without touching `docs/` or `tests/`.
  Enable once per clone: `git config core.hooksPath hooks`. Bypass once with `--no-verify`.

What can't be mechanized (accurate prose / a fresh session log) stays on the honor system —
but the hook reminds you, and CLAUDE.md tells new sessions to read the latest session log.

## Run it
`PYTHONPATH=. ARGUS_MODEL_URL=http://localhost:8099/v1 .venv/bin/python -m argus.main "what time is it?"`
(CLI one-shot). The UI server is `argus-ui.service` (anvil :8210); restart with
`systemctl --user restart argus-ui`. Forge "Claude Code" model = a persistent `claude`
session per conversation (`argus/claude_code.py`), bypassing llama-swap.
