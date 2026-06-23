# Issue Register

The single authoritative backlog of Deferred and Observation items. See
`docs/POLICY-scope-control.md` for how items get here and how they're triaged.
Recording an issue here is **not** authorization to work on it — triage happens at
task/milestone boundaries, ordered by Severity → Blast Radius → Dependency Impact →
Expected Value.

## Open

### Argus local-80B chat path is broken
```
category:          Deferred
severity:          Medium
status:            open
blast_radius:      Medium
component:         ui/server.py (model wiring) + llama-swap (anvil:9090)
discovered_during: Fix test_server.py (2026-06-22)
notes:             argus-ui sets ARGUS_MODEL_URL=http://localhost:8099/v1 but nothing
                   listens on :8099; the configured Qwen3-Next-80B is no longer served by
                   llama-swap (only gemma/gpt-oss/etc remain). Any non-claude-code model
                   pick connection-errors. Not biting because Shane uses the claude-code
                   model (separate path). Fix: find where :8099 went / re-add the 80B or
                   repoint at :9090. Tracked in memory: argus-local-80b-chat-broken.
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
                   NEEDS COMMIT: `git add argus/claude_code.py docs/postmortem-oauth-token-revocation.md`
                   then commit with message from the postmortem.
```
