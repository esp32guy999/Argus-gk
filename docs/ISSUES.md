# Issue Register

The single authoritative backlog of Deferred and Observation items. See
`docs/POLICY-scope-control.md` for how items get here and how they're triaged.
Recording an issue here is **not** authorization to work on it — triage happens at
task/milestone boundaries, ordered by Severity → Blast Radius → Dependency Impact →
Expected Value.

## Open

### Watchdog loop-nudges can crash the whole run via tool max_retries
```
found:             2026-07-05, ornith eval (restraint-danger task)
symptom:           watchdog raises ModelRetry on repeated calls; those count toward
                   the TOOL's max_retries=2 (registry.as_pydantic_tool) — a third
                   repeat escalates to "Tool 'list_dir' exceeded max retries" which
                   propagates as an exception and kills the run with a raw error
                   instead of a graceful give-up.
blast radius:      Medium — any model that triple-repeats a call gets a crash, not
                   an answer. UI shows an error bubble.
fix candidate:     catch UnexpectedModelBehavior in the loop driver and return
                   _budget_summary-style text ("stopped: repeated list_dir 3x"), or
                   have the watchdog short-circuit the run itself after N nudges.
                   Add an eval task that forces a triple-repeat to lock the fix.
```

### MCP-lane tool descriptions embed poorly for semantic select()
```
found:             2026-07-05, by the eval suite (evals/run.py --selection-only)
symptom:           "is anything printing on the P1S?" ranks GetLiveContext 16th of 65
                   (top-8 cut) — its HA-supplied description never mentions devices,
                   printers, or state; tags are just "home".
blast radius:      Low — home-state queries can still route via pinned lookup_memory,
                   but every MCP tool inherits whatever description the server ships,
                   so selection quality is capped by upstream docs.
fix candidate:     selection-side doc enrichment — SemanticSelector._doc() already
                   folds in tags; let lanes attach alias/example text (Tool.example)
                   and include it in _doc, or per-tool alias overrides in
                   config/mcp_servers.yaml. Measure on the eval suite before keeping.
```

### Audiobook lane down — ABB `/?s=` search returns the homepage
```
category:          Deferred
severity:          Medium
status:            open
blast_radius:      Medium (whole audiobook search/grab lane)
component:         argus/tools/audiobook.py (_abb_get / search via /?s=)
discovered_during: Grab "Empirical Endgame" (VGO) (2026-06-24)
notes:             audiobookbay.lu is REACHABLE from anvil (fetch returns ~34KB), but
                   GET /?s=<query> now returns the ABB HOMEPAGE (<title>"Unabridged
                   Audiobooks Free Online", 9 random recent posts) instead of search
                   results — for every query, incl. titles known to be on ABB, and after
                   a 5-min cooldown (so NOT rate-limiting). The query-match filter then
                   drops everything → search() returns []. Looks like an ABB-side change
                   (anti-bot/Cloudflare or a moved search path). Fix: find ABB's current
                   search mechanism (e.g. /page/N/?s=, a category path, or POST) and update
                   the scraper. Alt acquisition path: Readarr+Prowlarr indexers.
```


### z-engineer (image-manipulation model) shows in the chat model selector
```
category:          Observation
severity:          Low
status:            open
blast_radius:      Low
component:         ui/static/app.js (renderModelSelect / renderModelList) or ui/server.py /argus/models
discovered_during: Theme-per-model design (2026-06-23)
notes:             z-engineer is an image-manipulation model, not a chat model, so it
                   shouldn't appear in the model dropdown. app.js already special-cases
                   other media models by id regex (/^z-(image-edit|klein|video)/i and
                   /^z-(image|klein|video)/i) for timeouts/handling, but z-engineer isn't
                   caught by those and isn't filtered out. Fix options: (a) filter media
                   models out of the chat selector client-side, or (b) tag them in the
                   backend model cfg (e.g. cfg.kind="media") and filter on that — cleaner
                   and avoids brittle id-regex matching. Prefer (b).
```

### Image attach: follow-ups after the claude-code wiring — largely SUPERSEDED (2026-08-03)
Paperclip pipeline rewritten: `argus/attachments.py` materialises all uploads; GK gets
`--prompt-json` images; local models get OCR + paths; roundtable refuses visibly.
Residual observations only:
- [ ] History UI still does not re-show image thumbnails on reload (store keeps
      `[attached: name]` text, not the bytes). Low — path remains on disk under
      `~/.cache/argus/uploads/<cid>/`.
- [ ] Local VLM for messy photos when tesseract fails — deferred (vision_lane V4).

### Argus local-80B serving path — RESOLVED (was: broken)
```
category:          Deferred
severity:          Medium
status:            resolved (2026-06-23)
blast_radius:      Medium
component:         ui/server.py (model wiring) + llama-swap (anvil:9090)
discovered_during: Fix test_server.py (2026-06-22)
notes:             Original report (2026-06-22): ARGUS_MODEL_URL=:8099 with nothing
                   listening. Now stale — the argus-ui env sets
                   ARGUS_MODEL_URL=http://localhost:9090/v1, llama-swap serves
                   qwen3-next-80b at :9090, and a direct chat completion returns
                   cleanly (verified 2026-06-23). Both model-listing and inference
                   point at :9090. Residual nit: server.py line 22 still DEFAULTS to
                   :8099 — harmless (env overrides) but worth aligning the default.
                   STILL TO VALIDATE: the full in-harness path (loop.stream_run +
                   Pydantic AI tool loop), not just raw llama-swap — part of the 80B
                   onramp.
```

### lidarr_get_album can't reach metadata-profile-excluded albums
```
category:          Deferred
severity:          Low
status:            open
blast_radius:      Low
component:         argus/tools/arr_acquire.py
discovered_during: Grab Cyberpunk: Edgerunners soundtrack (2026-06-22)
notes:             Soundtracks/compilations are filtered out of an artist's discography by
                   the "Standard" metadata profile, so lidarr_get_album never finds them and
                   needed a manual POST /album by MBID. Fold a by-MBID fallback into the tool.
```

### Forge swipe-to-copy needs a sheet because clipboard API requires HTTPS
- [ ] Serve the Forge UI over HTTPS (`tailscale serve`) → enables `navigator.clipboard` for silent right-swipe copy — Observation — ui/static + deploy (from: swipe-to-copy, 2026-06-22)

## Resolved
- `test_server.py` stale `/brain/*` paths → `/argus/*` + model-backend skip — resolved 2026-06-22 (commit a69cad1)

### OAuth token revocation — isolated config dirs caused token races
```
category:          Post-mortem (resolved)
severity:          High
status:            resolved (2026-06-23)
blast_radius:      High — all Claude Code consumers on anvil
component:         argus/claude_code.py, ai-stack/claude-shim/server.py
notes:             Multiple `claude -p` processes used separate CLAUDE_CONFIG_DIR
                   paths (~/.claude-shim-config, ~/argus/.cchome, ~/.claude). Each
                   independently refreshed OAuth tokens, revoking the others. Auth
                   dropped every ~30 min. Fix: all processes now use ~/.claude
                   directly. Full analysis: docs/postmortem-oauth-token-revocation.md
                   Committed 2026-06-23 (commit 4661cd0). Monitoring to confirm
                   auth stays up; shim-side change lives in ai-stack repo (deploy separately).
```

### SQLite WAL unbounded — argus.db-wal grew to 4MB (2.6x main DB)
```
category:          Maintenance
severity:          Low
status:            open (found 2026-07-03 audit)
blast_radius:      Low — extra read I/O, slower restart replay
component:         argus/storage.py
notes:             No periodic checkpoint; WAL only truncates on last-connection
                   close, which a long-running service never does. Fix candidate:
                   PRAGMA wal_checkpoint(TRUNCATE) on a timer or every N writes
                   in Store. Not urgent at current sizes.
```

## web_search backend down — DDG 202, no Brave key (2026-07-10) — RESOLVED (2026-08-03)

```
status:            resolved (2026-08-03)
resolution:        Self-hosted SearXNG on glassgarden via Unraid Community Apps
                   (Kilrah template → my-SearXNG.xml, host :8089). Argus web_search
                   prefers SEARXNG_URL; Brave optional; DDG last resort with teaching
                   errors on HTTP 202. systemd: argus-ui.service.d/searxng.conf.
```

blast radius:      Medium — the research lane (Task 1) is hardened but its SEARCH
                   was non-functional; blocked Task 3's SOTA research round.
found:             Running the Task 3 research round via argus web_search — every
                   query returned "no results".
root cause:        html.duckduckgo.com answered the scrape path with HTTP 202
                   (anti-bot); Brave wanted a card for API signup. Both paths dead.
fix applied:      SearXNG on Unraid (not orphan: CA user template + Icon/WebUI).

- **CPU Gemma (gemma4e2b) crashes with the full toolset** (2026-07-10, Low): a chat
  turn to gemma4-cpu terminates its llama-server with
  `GGML_ASSERT(n_inputs < GGML_SCHED_MAX_SPLIT_INPUTS)` — the E2B backend can't handle
  ~84 tool schemas at once. Deferred: give gemma4-cpu a reduced tool selection (tag-
  or top-K-limited) before it's usable as an interactive tool-driver. GPU models are
  unaffected. Surfaced during the model-manifest smoke test.
