"""Contract test: flag_memory_candidate is a LOW-AUTHORITY capture (the fuel line).

The tool lets the agent flag a "might be worth remembering" moment into the cold D5
ledger (memory_candidates) — collect-now, curate-later (specs/memory_system.md §5). The
load-bearing promise Gemma named: flagging is ZERO-RISK. It lands a candidate and must
NOT write a memory_fact, must NOT trigger any lifecycle transition, must NOT confirm
anything. The ledger has no trust and no recall path, so a poisoned flag costs nothing
until consolidation (which applies the trust discipline) exists.

Offline + deterministic. Runnable standalone:  python tests/test_flag_candidate.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARGUS_DB"] = os.path.join(tempfile.mkdtemp(), "argus.db")


def main() -> int:
    from argus.storage import get_store
    from argus.tools.notes import flag_memory_candidate, tools

    store = get_store()
    assert store.list_memory_candidates() == [], "ledger not clean at start"

    res = flag_memory_candidate("User prefers metric units in all answers", kind="rule")

    # 1. it lands in the cold ledger, with its kind + provenance (+ importance)
    assert res.get("flagged") and res.get("id"), res
    cands = store.list_memory_candidates()
    assert len(cands) == 1, f"expected one candidate: {cands}"
    c = cands[0]
    assert c["kind"] == "rule" and "metric units" in c["summary"], c
    assert c["source"], f"no provenance on flagged candidate: {c}"
    assert c.get("importance") is not None and int(c["importance"]) >= 0, c
    print("PASS: flag lands in the cold D5 ledger with kind + provenance + importance")

    # 2. LOW-AUTHORITY (Gemma's core ask) — flagging touches NOTHING in the lifecycle.
    assert store.list_facts(include_history=True) == [], "flag wrote a memory_fact — bypassed lifecycle!"
    assert store.consolidation_log() == [], "flag triggered a lifecycle transition!"
    print("PASS: flag does NOT create a fact or any lifecycle transition (zero-authority)")

    # 3. unknown kinds can't smuggle in — coerced to 'other' (constrained vocabulary)
    # Use a high-value summary so the importance gate still accepts the write.
    r2 = flag_memory_candidate(
        "anvil ARGUS_PORT is 8210 (argus-ui)", kind="totally-made-up-kind")
    assert r2.get("flagged") and r2.get("id"), r2
    c2 = [x for x in store.list_memory_candidates() if x["id"] == r2["id"]][0]
    assert c2["kind"] == "other" or c2["kind"] == "config", f"unknown kind not coerced: {c2}"
    # kind may be reclassified to config by policy — either is fine; never invents free kinds
    assert c2["kind"] in (
        "dead_end", "correction", "repeat_lookup", "rule", "other",
        "config", "personal", "document", "research", "event",
    ), c2
    assert store.list_facts(include_history=True) == [], "second flag leaked into facts!"
    print("PASS: unknown kind coerced; still no fact side-effects")

    # 4. the tool is actually registered and non-actionable (safe to keep always-on)
    t = next((t for t in tools() if t.name == "flag_memory_candidate"), None)
    assert t is not None, "flag_memory_candidate not exposed by notes.tools()"
    assert t.provider != "web", "capture tool must not be a web-lane tool"
    print(f"PASS: tool registered (provider={t.provider!r}, non-actionable)")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
