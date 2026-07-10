"""Contract test for the memory-candidate ledger (research-lane D5, argus/storage.py).

Append-only collection of "might be worth remembering" moments — the empirical
dataset Task 3's write policy will be designed from. No retrieval, no curation, no
trust: this test pins only that candidates go in and come back, newest-first, with
unknown kinds coerced to 'other'.

Offline + deterministic (temp db). Runnable standalone:
    python tests/test_memory_candidates.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.storage import Store, MEMORY_CANDIDATE_KINDS

    db = os.path.join(tempfile.mkdtemp(), "cand.db")
    s = Store(db)

    # 1. append returns the row with a coerced-known kind
    r = s.add_memory_candidate("dead_end", "PETG temp query returned nothing useful",
                               conversation_id="c1", source="web_fetch:https://x")
    assert r["id"] and r["kind"] == "dead_end" and r["source"] == "web_fetch:https://x", r
    print("PASS: candidate appended with fields")

    # 2. unknown kind coerces to 'other' (never rejects — collection must not lose data)
    r2 = s.add_memory_candidate("wharrgarbl", "odd one")
    assert r2["kind"] == "other", r2
    print("PASS: unknown kind coerced to 'other'")

    # 3. newest-first ordering
    s.add_memory_candidate("correction", "third")
    rows = s.list_memory_candidates()
    assert [x["summary"] for x in rows][:1] == ["third"], rows
    assert len(rows) == 3, rows
    print("PASS: list is newest-first and complete")

    # 4. kind filter
    only = s.list_memory_candidates(kind="dead_end")
    assert len(only) == 1 and only[0]["kind"] == "dead_end", only
    print("PASS: kind filter works")

    # 5. survives reopen (durable / append-only)
    s2 = Store(db)
    assert len(s2.list_memory_candidates()) == 3, "ledger did not persist"
    print("PASS: ledger persists across reopen")

    assert "other" in MEMORY_CANDIDATE_KINDS
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
