"""Contract test for argus/loop_guard.py — the loop safety core.

Offline; redirects state/audit into a temp dir and drives the pure decision logic.
Covers: master switch OFF, user-waiting yield, actionable-job filtering (agent vs
external/blocked/proposed), iteration cap, token budget, and the record() counter
(continue increments, stop resets) + audit append. Run: python tests/test_loop_guard.py
"""
from __future__ import annotations
import os, sys, tempfile, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from argus import loop_guard as lg                                 # noqa: E402

FAILS = []
def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond: FAILS.append(msg)


def main():
    d = tempfile.mkdtemp(prefix="loopguard_")
    lg._STATE_DIR = d
    lg._STATE_FILE = os.path.join(d, "state.json")
    lg._AUDIT_FILE = os.path.join(d, "audit.jsonl")

    AGENT = {"id": "a1", "category": "agent", "state": "active", "title": "Build X"}
    EXT = {"id": "e1", "category": "external", "state": "active", "title": "DL"}
    BLOCKED = {"id": "a2", "category": "agent", "state": "blocked", "title": "Y"}
    PROPOSED = {"id": "a3", "category": "agent", "state": "proposed", "title": "Z"}

    # actionable filter: only live agent jobs
    act = {j["id"] for j in lg.actionable_jobs([AGENT, EXT, BLOCKED, PROPOSED])}
    check(act == {"a1"}, "actionable = live agent jobs only (excludes external/blocked/proposed)")

    # master switch OFF -> never continue, regardless of work
    lg.ENABLED = False
    check(lg.decide([AGENT]).cont is False, "disabled -> stop")

    lg.ENABLED = True
    # user owed a reply -> yield
    check(lg.decide([AGENT], user_waiting=True).cont is False, "user_waiting -> stop")
    # no actionable work -> stop
    check(lg.decide([EXT, BLOCKED]).cont is False, "no agent work -> stop")
    # happy path -> continue with a prompt referencing the job
    dec = lg.decide([AGENT], now=1000.0)
    check(dec.cont and dec.job_id == "a1" and dec.prompt and "a1" in dec.prompt,
          "actionable + guardrails OK -> continue with job prompt")

    # success_condition from payload is woven into the prompt
    sc_job = {"id": "a9", "category": "agent", "state": "queued", "title": "SC",
              "payload": {"success_condition": "tests pass"}}
    check("tests pass" in (lg.decide([sc_job]).prompt or ""), "success_condition appears in prompt")

    # iteration cap: drive consecutive up to the limit
    lg.MAX_CONSECUTIVE = 3
    lg._save_state({"consecutive": 3, "window_start": 1000.0, "est_tokens": 0})
    capped = lg.decide([AGENT], now=1000.0)
    check(capped.cont is False and "cap" in capped.reason, "iteration cap -> stop")

    # token budget
    lg.MAX_CONSECUTIVE = 25
    lg.EST_TOKEN_BUDGET = 100
    lg._save_state({"consecutive": 0, "window_start": 1000.0, "est_tokens": 200})
    over = lg.decide([AGENT], now=1000.0)
    check(over.cont is False and "budget" in over.reason, "token budget -> stop")

    # live session-token measurement (from the hook) overrides the windowed counter
    lg._save_state({"consecutive": 0, "window_start": 1000.0, "est_tokens": 0})
    check(lg.decide([AGENT], now=1000.0, est_session_tokens=200).cont is False,
          "live token measurement over budget -> stop")
    check(lg.decide([AGENT], now=1000.0, est_session_tokens=10).cont is True,
          "live token measurement under budget -> continue")

    # window reset: an elapsed window zeroes the counters
    lg.EST_TOKEN_BUDGET = 4_000_000
    lg._save_state({"consecutive": 99, "window_start": 0.0, "est_tokens": 999})
    fresh = lg.decide([AGENT], now=1_000_000.0)   # far past window
    check(fresh.cont is True, "elapsed window resets counters -> continue")

    # record(): continue increments, stop resets; audit gets a line
    lg._save_state({"consecutive": 0, "window_start": 1000.0, "est_tokens": 0})
    s = lg.record(lg.Decision(True, "x", "p", "a1"), est_tokens_added=50, now=1000.0)
    check(s["consecutive"] == 1 and s["est_tokens"] == 50, "record(continue) bumps counters")
    s = lg.record(lg.Decision(False, "done", None, "a1"), now=1000.0)
    check(s["consecutive"] == 0, "record(stop) resets consecutive")
    lines = open(lg._AUDIT_FILE).read().strip().splitlines()
    check(len(lines) == 2 and json.loads(lines[0])["cont"] is True, "audit appends one line per decision")

    print(f"\n{'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
