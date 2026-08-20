"""SS is not a product. SEE is the only supervisor.

python tests/test_ss.py
"""
from __future__ import annotations
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="ss-gone-")
os.environ["ARGUS_DB"] = os.path.join(_TMP, "ss.db")
os.environ["ARGUS_SS"] = "1"  # even if someone turns it on, the hook is gone


def main() -> int:
    import argus.storage as storage
    storage._STORE = None
    from argus.see import api
    from argus.see.models import SeeEvent

    if hasattr(api, "_ss_shadow"):
        print("FAIL: _ss_shadow still exists — SS is not a second supervisor")
        return 1
    print("PASS: no _ss_shadow hook")

    task = api.create(
        "SS retired probe",
        success_criteria=["done"],
        checklist=["step"],
        start=True,
    )
    before = task.current_state
    dec = api.tick(task.id)
    after = api.get(task.id)
    if "ss_shadow" in (dec.details or {}):
        print(f"FAIL: tick still attaches ss_shadow: {dec.details}")
        return 1
    if after is None or after.current_state != before:
        print("FAIL: tick mutated SEE state without a stall")
        return 1
    print("PASS: tick has no ss_shadow and does not mutate on a quiet tick")

    dec2 = api.on_event(task.id, SeeEvent(
        type="ToolCalled", detail="web_search",
        data={"tool": "web_search", "args_key": '{"q":"x"}'},
    ))
    if "ss_shadow" in (dec2.details or {}):
        print("FAIL: on_event still attaches ss_shadow")
        return 1
    print("PASS: on_event has no ss_shadow")

    print("\nALL SS-RETIRED TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
