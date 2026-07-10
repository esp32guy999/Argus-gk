"""Contract test: B2 — every durable save enters the fact lifecycle (no god-mode write).

Shane's B2 call (2026-07-10): nothing is born permanent. `save_note` no longer performs
an unmanaged, instantly-authoritative write — it creates a `memory_facts` row in state
`proposed`, carrying provenance, that must earn durability through `transition_fact()`.

Crucially, recall is NOT regressed: the note is still persisted/indexed for lookup_memory
(the save→recall loop notes.py exists to close). That anti-regression check is the gap in
the "just reroute save_note" plan — the fact row and the recall index are separate
substrates, so B2 is TWO coordinated writes, not one.

Gemma's two named tests are T1 and T2 below.

Offline + deterministic. Runnable standalone:  python tests/test_note_lifecycle.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate the store + notes dir BEFORE importing anything that reads them.
_tmp = tempfile.mkdtemp()
os.environ["ARGUS_DB"] = os.path.join(_tmp, "argus.db")
os.environ["ARGUS_NOTES_DIR"] = os.path.join(_tmp, "notes")


def main() -> int:
    from argus.storage import get_store
    from argus.tools.notes import save_note

    store = get_store()
    assert store.list_facts(include_history=True) == [], "store not clean at start"

    NOTE = "PETG on the P1S: nozzle 240C, bed 70C, dry 65C/6h."
    res = save_note(NOTE)

    # T1 (Gemma) — save_note routes through the lifecycle: born 'proposed', not durable.
    assert res.get("fact_id"), f"save_note didn't create a lifecycle fact: {res}"
    facts = store.list_facts(include_history=True)
    assert len(facts) == 1, f"expected exactly one fact: {facts}"
    fact = facts[0]
    assert fact["state"] == "proposed", f"note not born 'proposed': {fact}"
    assert NOTE in fact["value"], fact
    assert store.list_facts(state="confirmed") == [], "note born confirmed — B2 bypassed!"
    print("PASS: save_note creates a 'proposed' fact, not an instant durable write")

    # provenance recorded (origin is the tool, never spoofed as a trusted 'user')
    assert fact["source"] and fact["source"] != "user", f"bad/absent provenance: {fact}"
    print(f"PASS: provenance recorded, not spoofed as user (source={fact['source']!r})")

    # anti-regression — recall path preserved: the note is still persisted for lookup.
    assert res.get("file"), f"recall persistence dropped — save->recall loop broken: {res}"
    note_files = os.listdir(os.environ["ARGUS_NOTES_DIR"])
    assert any(f.endswith(".md") for f in note_files), f"no note file written: {note_files}"
    print("PASS: recall not regressed (note still persisted alongside the fact)")

    # T2 (Gemma) — the saved note moves through the lifecycle via the state machine.
    fid = res["fact_id"]
    store.observe_fact(fid, reason="seen again in conversation")
    assert store.get_fact(fid)["state"] == "observed", store.get_fact(fid)
    store.transition_fact(fid, "confirmed", reason="user affirmed the setting", signal="user")
    assert store.get_fact(fid)["state"] == "confirmed"
    print("PASS: saved note transitions proposed → observed → confirmed")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
