# Post-Mortem: OAuth Token Revocation (2026-06-16 → 2026-06-23)

**Severity:** High — auth dropped every ~30 minutes, making Claude Code unusable  
**Duration:** ~7 days intermittent  
**Resolved:** 2026-06-23  

## Symptom

Claude Code (via claude-shim and Argus `claude_code.py`) would authenticate
successfully, work for a few messages, then return `401 Invalid authentication
credentials`. The token's `expiresAt` field claimed 7+ hours remaining, but
Anthropic had revoked it server-side.

## Root Cause

**Multiple `claude -p` processes with separate credential files were racing on
OAuth token refresh. When one process refreshed, Anthropic revoked the old token
— which the other processes were still using.**

The architecture had **3 independent credential stores**:

| Consumer | `CLAUDE_CONFIG_DIR` | Credential file |
|----------|-------------------|-----------------|
| claude-shim (OpenAI wrapper) | `~/.claude-shim-config/` | own copy |
| Argus `claude_code.py` | `~/argus/.cchome/` | own copy |
| Manual `claude` CLI / relogin | `~/.claude/` (default) | canonical |

Each consumer synced credentials FROM `~/.claude/` on subprocess spawn (one-way
copy), then the subprocess would independently refresh its token and write it
back to its own isolated `CLAUDE_CONFIG_DIR`. This created a **token fan-out**:

1. Process A refreshes → gets new token A', old token revoked
2. Process B still holds old token → 401
3. Process B refreshes → gets new token B', token A' revoked
4. Process A now 401s → cycle continues

Additionally, orphaned `claude -p` processes from crashed/restarted services
would linger and refresh tokens unpredictably, making the problem worse.

## Timeline

- **Jun 16**: `claude -p` blocked by Anthropic, switched Hermes to Ollama. First
  auth instability noticed on remaining CC consumers.
- **Jun 22**: Rewrote claude-shim from per-request subprocess spawning (peace
  pattern) to persistent session. Reduced token churn but didn't fix the
  multi-credential-file race.
- **Jun 22**: Changed shim `CONFIG_DIR` to `~/argus/.cchome/` — reduced from 3
  credential files to 2, but race still existed with `~/.claude/`.
- **Jun 23**: Identified the actual root cause — divergent credential files.
  Fixed by eliminating all isolated config dirs.

## Fix Applied

**Removed all isolated `CLAUDE_CONFIG_DIR` usage.** Both `claude_code.py` and
the claude-shim now let `claude -p` use `~/.claude/` directly (the default).

### Changes to `argus/claude_code.py`:
- `CONFIG_DIR` default changed from `~/argus/.cchome/` → `~/.claude/`
- `_sync_credentials()` → no-op (no sync needed when using canonical path)
- `_clean_env()` → removed `env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR` line;
  now pops `CLAUDE_CONFIG_DIR` so subprocess inherits the default `~/.claude/`
- `_transcript_exists()` → hardcoded `~/.claude` path for session lookup
- Removed unused `import shutil`

### Changes to `ai-stack/claude-shim/server.py` (not in this repo):
- Same pattern: removed `CONFIG_DIR`, `_sync_credentials()`, and
  `CLAUDE_CONFIG_DIR` override in `_clean_env()`
- Already uses persistent session (peace pattern) — one `claude -p` subprocess
  for all requests, not one per request

### Operational:
- Killed orphan `claude -p` process (PID 2977109) from previous shim crash
- Synced newest valid token to `~/.claude/.credentials.json`
- Restarted both `claude-shim` and `argus-ui` services

## How to Verify

```bash
# Should see exactly 0-2 claude -p processes (shim + argus CC, only when active)
ps aux | grep "claude -p" | grep -v grep

# None should have CLAUDE_CONFIG_DIR set
for pid in $(pgrep -f "claude -p"); do
  echo "PID $pid:"; cat /proc/$pid/environ | tr '\0' '\n' | grep CLAUDE_CONFIG || echo "  (default ~/.claude — correct)"
done

# All credential files should be identical (or only ~/.claude should exist)
md5sum ~/.claude/.credentials.json ~/argus/.cchome/.credentials.json ~/.claude-shim-config/.credentials.json 2>/dev/null
```

## Prevention

1. **One credential file.** Never copy credentials to isolated dirs. All
   `claude -p` subprocesses must use `~/.claude/` (the default).
2. **No orphan processes.** After restarting services, verify no stale `claude -p`
   processes linger: `pgrep -af "claude -p"`. Kill any that aren't children of
   a running service.
3. **Relogin writes to `~/.claude/`.** The `relogin.py` module already does this
   correctly — it runs `claude auth login` which writes to the default path.
4. **If adding a new CC consumer**, do NOT set `CLAUDE_CONFIG_DIR`. Let it use
   the default. The `_clean_env()` pattern (stripping `CLAUDE_CODE*` vars) is
   sufficient to avoid child-session detection.

## Commit Checklist

The `argus/claude_code.py` change is already on disk. To commit:

```bash
cd ~/argus
git add argus/claude_code.py
git commit -m "fix(claude_code): use ~/.claude directly — stop token revocation races

Isolated CLAUDE_CONFIG_DIR copies diverged on refresh, causing Anthropic
to revoke tokens the other processes were using. Now all claude -p
subprocesses share ~/.claude (the default) so refreshes are coherent.

See docs/postmortem-oauth-token-revocation.md for full analysis."
```
