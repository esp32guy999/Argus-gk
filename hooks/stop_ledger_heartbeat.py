#!/usr/bin/env python3
"""Claude Code **Stop hook** — the "Ralph"/hook-loop heartbeat (docs/DESIGN-work-ledger.md).

Fires when the agent finishes a message. Reads the Argus ledger, and if there is
genuine *agent-owned, actionable* work, asks Claude Code to continue by injecting a
pre-written prompt — otherwise lets it stop and hand control back. All the safety
(iteration cap, token budget, audit, master switch) lives in argus.loop_guard.

SHIPPED OFF. Inert until ARGUS_LOOP_ENABLED=1 is set, because loop_guard.decide()
returns "stop" whenever the switch is off. To enable, see hooks/README.md.

Protocol: read hook JSON on stdin; to CONTINUE, print {"decision":"block","reason":<prompt>}
and exit 0 (Claude Code treats a blocked Stop as "keep going with this reason"); to
STOP, exit 0 with no decision. Any error -> exit 0 (fail-safe: never trap the agent).
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request

# self-contained: don't depend on PYTHONPATH being set by the harness
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARGUS = os.environ.get("ARGUS_URL", "http://127.0.0.1:8210")


def _ledger_jobs() -> list[dict]:
    url = f"{ARGUS}/argus/jobs?include_terminal=false"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r).get("jobs", [])


def main() -> None:
    # Read (and ignore-on-error) the hook payload; we don't currently need its fields,
    # but consuming stdin keeps the harness happy.
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        from argus import loop_guard
    except Exception:
        return  # can't load guardrails -> never auto-continue

    # Fast path: if the master switch is off, don't even hit the network.
    if not loop_guard.ENABLED:
        return

    try:
        jobs = _ledger_jobs()
    except Exception:
        return  # ledger unreachable -> fail safe, allow stop

    decision = loop_guard.decide(jobs)
    loop_guard.record(decision)
    if decision.cont and decision.prompt:
        print(json.dumps({"decision": "block", "reason": decision.prompt}))
    # else: no output, exit 0 -> allow the agent to stop


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute backstop: a Stop hook must NEVER crash the turn or trap the agent.
        pass
    sys.exit(0)
