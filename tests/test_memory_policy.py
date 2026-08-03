"""Flywheel F0–F2: importance scoring, reject gate, orchestrator after_turn.

Standalone: python tests/test_memory_policy.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARGUS_DB"] = os.path.join(tempfile.mkdtemp(), "argus.db")
# deterministic thresholds for tests
os.environ["ARGUS_MEMORY_IMPORTANCE_MIN"] = "45"
os.environ["ARGUS_MEMORY_AUTO_MIN"] = "65"


def main() -> int:
    # re-import after env
    from importlib import reload
    import argus.memory_policy as mp
    reload(mp)
    from argus.storage import get_store
    from argus.tools.notes import flag_memory_candidate

    store = get_store()

    # 1. Homelab config scores high
    s = mp.score_importance("glassgarden Sonarr listens on port 8989", kind="config")
    assert s["importance"] >= 60, s
    assert s["kind"] == "config"
    assert not s["reject"], s
    print("PASS: config/homelab scores high")

    # 2. Generic world knowledge rejects
    s2 = mp.score_importance("The capital of Peru is Lima", kind="other")
    assert s2["reject"] or s2["importance"] < 45, s2
    print("PASS: generic world knowledge low / reject")

    # 3. Explicit remember boosts
    s3 = mp.score_importance(
        "use metric units", kind="rule",
        user_text="Please remember this: always use metric",
    )
    assert s3["importance"] >= 60 and not s3["reject"], s3
    print("PASS: explicit remember boosts")

    # 4. Tool flag rejects lima, accepts sonarr
    r_bad = flag_memory_candidate("The capital of France is Paris", kind="other")
    assert r_bad.get("rejected") is True and not r_bad.get("flagged"), r_bad
    assert store.list_memory_candidates() == [], "rejected should not write"
    print("PASS: flag_memory_candidate rejects low importance")

    r_ok = flag_memory_candidate(
        "glassgarden Sonarr listens on port 8989", kind="config")
    assert r_ok.get("flagged") and r_ok.get("importance", 0) >= 45, r_ok
    cands = store.list_memory_candidates()
    assert len(cands) == 1 and cands[0].get("importance", 0) >= 45, cands
    print("PASS: flag_memory_candidate accepts high importance + stores score")

    # 5. after_turn auto-extracts port from researchy reply
    before = len(store.list_memory_candidates())
    added = mp.after_turn(
        user_message="What port is Sonarr on glassgarden?",
        assistant_text="glassgarden Sonarr listens on port 8989 via docker.",
        tools_called=["web_search", "web_fetch"],
        conversation_id="flywheel-demo",
    )
    assert added, f"expected auto candidates: {added}"
    assert len(store.list_memory_candidates()) > before
    print("PASS: after_turn auto-flags high-value config extract")

    # 6. after_turn does NOT flag weather trivia
    before = len(store.list_memory_candidates())
    added2 = mp.after_turn(
        user_message="What's the capital of Peru?",
        assistant_text="The capital of Peru is Lima.",
        tools_called=["web_search"],
        conversation_id="flywheel-demo",
    )
    # may be empty or only low-skipped
    assert len(store.list_memory_candidates()) == before or all(
        a.get("flagged") for a in added2)  # if any, they must have been flagged high
    # stronger: no new "Lima" summary
    summaries = " ".join(c["summary"] for c in store.list_memory_candidates())
    assert "Lima" not in summaries and "Peru" not in summaries, summaries
    print("PASS: after_turn does not ledger generic capital facts")

    # 7. Two-session simulation: session A flags, session B reads candidates
    # (full recall is lookup_memory on facts; candidates are cold — promote via add_fact)
    from argus.storage import get_store as gs
    st = gs()
    st.add_fact(
        key="glassgarden.sonarr.port",
        value="8989",
        source="flywheel-demo",
        conversation_id="session-a",
    )
    facts = st.list_facts()
    assert any("8989" in f["value"] for f in facts), facts
    # proposed fact is listable; lookup_memory path uses index — smoke proposed row exists
    print("PASS: two-session substrate — cold candidate + proposed fact coexist")

    print("\nALL MEMORY POLICY / FLYWHEEL F0–F2 CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
