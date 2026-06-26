# Argus hooks

## `stop_ledger_heartbeat.py` — the hook/"Ralph" loop (SHIPPED OFF)

A Claude Code **Stop hook**: when the agent finishes a message, it checks the Argus
work-ledger and, if there's genuine *agent-owned, actionable* work, injects a
pre-written prompt to keep going — the automated version of Shane pinging "Status?"
to stop the agent stalling. All safety lives in `argus/loop_guard.py`
(iteration cap, token budget, rolling window, audit log, master switch).

### It is OFF by default — and intentionally not wired into settings.json
A Stop hook governs the *live* session, so a bad one can trap the agent (we already
paid for one crash-loop — see `docs/postmortem-ledger-migration-crashloop.md`). So
enabling is a deliberate, two-part opt-in:

**1. Register the hook** in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "/home/shane/argus/.venv/bin/python /home/shane/argus/hooks/stop_ledger_heartbeat.py" } ] }
    ]
  }
}
```
Even registered, it stays inert: `loop_guard.decide()` returns "stop" whenever the
switch is off, so the hook just consumes stdin and exits.

**2. Flip the master switch** by setting the env for the session/service that runs
Claude Code:
```
ARGUS_LOOP_ENABLED=1
```
Optional overrides (defaults shown): `ARGUS_LOOP_MAX_CONSECUTIVE=25`,
`ARGUS_LOOP_WINDOW_SEC=3600`, `ARGUS_LOOP_TOKEN_BUDGET=4000000`,
`ARGUS_URL=http://127.0.0.1:8210`, `ARGUS_LOOP_DIR=~/.claude`.

### What makes it continue (all must hold)
- master switch on, and the user isn't owed a reply, **and**
- a ledger job with `category=agent` in state `queued`/`active` (not blocked/proposed/external), **and**
- under the iteration cap and token budget for the rolling window.

Otherwise it allows the stop. Every decision is appended to
`~/.claude/argus-loop-audit.jsonl`.

### Kill switch
Unset `ARGUS_LOOP_ENABLED` (or set it to `0`), or remove the Stop block from
settings.json. To halt mid-run: `echo '{"consecutive":9999,"window_start":0}' >
~/.claude/argus-loop-state.json` trips the cap on the next stop.

### Test without enabling the live hook
```
echo '{}' | ARGUS_LOOP_ENABLED=1 ARGUS_LOOP_DIR=/tmp/lg python hooks/stop_ledger_heartbeat.py
```
With an `agent` job active in the ledger it prints a `{"decision":"block",...}`; with
none, it prints nothing (allows stop).
