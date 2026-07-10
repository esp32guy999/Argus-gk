"""Contract test for the memory fact state machine (Task 3 mechanism, storage.py).

The evidence-independent mechanism from specs/memory_system.md §6a/§6b/§7/§11:
a fact lifecycle (proposed→observed→confirmed→superseded/invalidated) with the
poisoning-by-repetition defense (§6b: confirmation needs a TRUSTED signal, never a
mention count) and an append-only consolidation_log (§7: nothing destructive, history
kept for audit). NO promotion thresholds are hard-coded — that's data-derived (§11).

Two of these are the tests Gemma named explicitly:
 - a Proposed memory can't become Confirmed just by being mentioned N times.
 - an Invalidated memory stays in history for audit.

Offline + deterministic (temp db). Runnable standalone:
    python tests/test_memory_state_machine.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.storage import Store, FACT_STATES, TRUSTED_SIGNALS

    db = os.path.join(tempfile.mkdtemp(), "mem.db")
    s = Store(db)

    # 0. a new fact is born 'proposed'
    f = s.add_fact("printer.ip", "192.168.4.31", source="user")
    assert f["state"] == "proposed", f
    print("PASS: new fact is born 'proposed'")

    # 1. GEMMA'S TEST #1 — repetition alone can NEVER confirm (poisoning defense §6b)
    for _ in range(5):
        s.observe_fact(f["id"], reason="mentioned again")
    cur = s.get_fact(f["id"])
    assert cur["state"] != "confirmed", f"5 mentions confirmed a fact! {cur}"
    assert cur["mention_count"] >= 5, cur
    # and an explicit attempt to confirm on an untrusted 'mention' signal is refused
    try:
        s.transition_fact(f["id"], "confirmed", reason="mentioned a lot", signal="mention")
        print("FAIL: confirmed on an untrusted signal"); return 1
    except ValueError:
        pass
    print("PASS: repetition/mention never reaches 'confirmed'")

    # 1b. confirmation DOES work on a trusted signal
    s.transition_fact(f["id"], "confirmed", reason="user affirmed", signal="user")
    assert s.get_fact(f["id"])["state"] == "confirmed"
    assert "user" in TRUSTED_SIGNALS
    print("PASS: a trusted signal confirms")

    # 2. GEMMA'S TEST #2 — invalidated facts stay in history for audit
    g = s.add_fact("ha.host", "freya", source="tool")
    s.transition_fact(g["id"], "invalidated", reason="freya retired; HA moved to nyx",
                      signal="user")
    assert s.get_fact(g["id"])["state"] == "invalidated", "invalidated fact vanished"
    hist = {x["id"] for x in s.list_facts(include_history=True)}
    active = {x["id"] for x in s.list_facts(include_history=False)}
    assert g["id"] in hist, "invalidated fact missing from history view"
    assert g["id"] not in active, "invalidated fact clutters the active view"
    print("PASS: invalidated fact retained in history, hidden from active recall")

    # 3. every transition is recorded in the append-only consolidation_log (§7)
    log = s.consolidation_log(fact_id=g["id"])
    assert any(e["to_state"] == "invalidated" and e["reason"] for e in log), log
    print("PASS: transitions are logged (auditable/reversible)")

    # 4. invalid transitions are refused (state machine is enforced)
    try:
        s.transition_fact(g["id"], "confirmed", reason="revive", signal="user")  # invalidated is terminal
        print("FAIL: transitioned out of terminal 'invalidated'"); return 1
    except ValueError:
        pass
    print("PASS: illegal transitions refused (invalidated is terminal)")

    # 5. durable across reopen
    s2 = Store(db)
    assert s2.get_fact(f["id"])["state"] == "confirmed"
    assert len(s2.list_facts(include_history=True)) == 2
    assert "proposed" in FACT_STATES
    print("PASS: state + log persist across reopen")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
