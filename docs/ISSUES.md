# Issue Register

The single authoritative backlog of Deferred and Observation items. See
`docs/POLICY-scope-control.md` for how items get here and how they're triaged.
Recording an issue here is **not** authorization to work on it — triage happens at
task/milestone boundaries, ordered by Severity → Blast Radius → Dependency Impact →
Expected Value.

## Open

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

### Image attach: follow-ups after the claude-code wiring
- [ ] Attachments aren't persisted to the conversation store — only the message text is
      saved (server.py add_message). On reload the user bubble loses its image thumbnail and
      the model can't re-see it. Observation — ui/server.py + store (from: image-attach, 2026-06-23)
- [ ] Non-image files (PDF, etc.) are staged + POSTed but not forwarded — `_image_block`
      only handles images. PDFs could go as Anthropic `document` blocks if CC stream-json
      accepts them. Observation — argus/claude_code.py (from: image-attach, 2026-06-23)
- [ ] Local-model path doesn't forward images (vision varies; 80B path separately broken).
      Folds into the local-80B fix below. Observation — ui/server.py (from: image-attach, 2026-06-23)

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
