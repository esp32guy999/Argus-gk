"""Contract test: recall honors the fact lifecycle (B2 read-side).

The write side lands notes as 'proposed' facts (test_note_lifecycle). This closes the loop
on the READ side, per specs/memory_system.md §6a — making the states actually *do*
something at retrieval time:
 - invalidated / superseded facts are DROPPED from recall (a known-false or replaced
   memory is never recalled as truth — the real safety payoff of the state machine).
 - proposed / observed facts recall but are flagged 'unconfirmed' so the agent treats
   them with skepticism instead of false certainty.
 - confirmed facts and curated docs recall normally (no regression).

Offline + deterministic: a stub embedder stands in for the anvil:8091 embeddings server,
so ranking is reproducible without the network.
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["ARGUS_DB"] = os.path.join(_tmp, "argus.db")
os.environ["ARGUS_NOTES_DIR"] = os.path.join(_tmp, "notes")

_VOCAB = ("petg", "nozzle", "bed", "router", "port", "channel")


def _stub_embed(texts):
    """Deterministic bag-of-marker-words vector; +1 bias so nothing is a zero vector."""
    return [[float(t.lower().count(w)) for w in _VOCAB] + [1.0] for t in texts]


def main() -> int:
    from argus import memory
    from argus.memory import DocMemory
    from argus.storage import get_store
    from argus.tools.notes import save_note
    from argus.tools.native import lookup_memory

    memory._INDEX = DocMemory(chunks=[], vectors=[], embed_fn=_stub_embed)  # offline index
    store = get_store()

    petg = save_note("PETG on the P1S: nozzle 240C, bed 70C.")
    router = save_note("Router admin is on port 8080, channel 6.")

    # sanity: a proposed note is recallable at all
    hits = lookup_memory("PETG nozzle bed")
    petg_hit = next((h for h in hits if "PETG" in h.get("text", "")), None)
    assert petg_hit is not None, f"proposed note not recalled: {hits}"

    # Test 2 (Framing) — proposed fact recalls, flagged unconfirmed.
    assert petg_hit.get("unconfirmed") is True, f"proposed recall not flagged: {petg_hit}"
    assert petg_hit.get("state") == "proposed", petg_hit
    print("PASS: proposed fact recalls, flagged 'unconfirmed'")

    # Test 1 (Filter) — invalidate PETG → it vanishes from recall entirely.
    store.transition_fact(petg["fact_id"], "invalidated", reason="wrong temps", signal="user")
    hits = lookup_memory("PETG nozzle bed")
    assert not any("PETG" in h.get("text", "") for h in hits), \
        f"invalidated fact still recalled as truth: {hits}"
    print("PASS: invalidated fact is dropped from recall")

    # Anti-regression — a confirmed fact still recalls, unflagged; the filter didn't nuke all.
    store.transition_fact(router["fact_id"], "confirmed", reason="verified", signal="user")
    hits = lookup_memory("router port channel")
    rhit = next((h for h in hits if "Router" in h.get("text", "")), None)
    assert rhit is not None, f"confirmed fact wrongly dropped: {hits}"
    assert not rhit.get("unconfirmed"), f"confirmed fact flagged unconfirmed: {rhit}"
    assert rhit.get("state") == "confirmed", rhit
    print("PASS: confirmed fact recalls normally, unflagged (no regression)")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
