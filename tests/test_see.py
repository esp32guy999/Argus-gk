"""SEE contract tests — state machine, verify/evidence, stall, persistence.

Standalone: python tests/test_see.py
"""
from __future__ import annotations
import os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARGUS_DB"] = os.path.join(tempfile.mkdtemp(), "see.db")
os.environ["ARGUS_SEE_IDLE_SEC"] = "2"  # short idle for stall test
os.environ["ARGUS_SEE_LOOP_REPEAT"] = "3"


def main() -> int:
    # fresh import after env
    import argus.storage as storage
    storage._STORE = None
    from argus.see import engine, api
    from argus.see.models import SeeEvent

    # 1. Happy path: plan → evidence → verify → COMPLETED
    task = engine.create_task(
        "Bring Sonarr up",
        success_criteria=["container running", "port responds"],
        checklist=["start container", "check port"],
    )
    engine.start_planning(task)
    engine.accept_plan(task)
    assert task.current_state == "EXECUTING", task.current_state

    engine.apply_event(task, SeeEvent(
        type="CheckpointReached", detail="started",
        data={"checklist_item": "start container"},
    ))
    engine.apply_event(task, SeeEvent(
        type="CheckpointReached", detail="probed",
        data={"checklist_item": "check port"},
    ))
    engine.apply_event(task, SeeEvent(
        type="EvidenceAdded", detail="container running",
        data={"criterion": "container running", "kind": "docker",
              "summary": "docker ps shows sonarr"},
    ))
    engine.apply_event(task, SeeEvent(
        type="EvidenceAdded", detail="port responds",
        data={"criterion": "port responds", "kind": "http",
              "summary": "HTTP 200"},
    ))
    dec = engine.request_verify(task)
    assert dec.action == "COMPLETE" and task.current_state == "COMPLETED", (dec, task.current_state)
    print("PASS: happy path COMPLETED with evidence")

    # 2. Verify without evidence fails back to EXECUTING
    t2 = engine.create_task("X", success_criteria=["done"])
    engine.accept_plan(t2)
    # accept_plan from NEW goes PLANNING→EXECUTING; create starts NEW then accept_plan needs PLANNING
    # create_task is NEW; accept_plan handles NEW→PLANNING implicit
    dec2 = engine.request_verify(t2)
    assert dec2.action == "RETRY" and t2.current_state == "EXECUTING", (dec2, t2.current_state)
    assert "missing evidence" in (dec2.feedback or "").lower()
    print("PASS: verify without evidence returns EXECUTING")

    # 3. Loop detection → STALLED
    t3 = engine.create_task("loop", success_criteria=["x"])
    engine.accept_plan(t3)
    for _ in range(3):
        engine.apply_event(t3, SeeEvent(
            type="ToolCalled", detail="list_dir",
            data={"args_key": '{"path":"/"}'},
        ))
    assert t3.current_state == "STALLED", t3.current_state
    print("PASS: tool loop → STALLED")

    # 4. Idle tick → STALLED
    t4 = engine.create_task("idle", success_criteria=["x"])
    engine.accept_plan(t4)
    t4.last_progress_ts = time.time() - 10
    t4.last_event_ts = t4.last_progress_ts
    dec4 = engine.tick(t4, now=time.time())
    assert t4.current_state == "STALLED" and dec4.new_state == "STALLED", (dec4, t4.current_state)
    print("PASS: idle tick → STALLED")

    # 5. Persistence via api
    storage._STORE = None
    t5 = api.create(
        "Persist me",
        success_criteria=["a", "b"],
        checklist=["a", "b"],
        conversation_id="conv-see-1",
    )
    assert t5.current_state == "EXECUTING"
    loaded = api.get(t5.id)
    assert loaded and loaded.goal == "Persist me"
    active = api.active_for_conversation("conv-see-1")
    assert active and active.id == t5.id
    api.checkpoint(t5.id, "did a", checklist_item="a")
    api.add_evidence(t5.id, "a", "ok", kind="test")
    api.add_evidence(t5.id, "b", "ok", kind="test")
    api.checkpoint(t5.id, "did b", checklist_item="b")
    d5 = api.request_verify(t5.id)
    assert d5.action == "COMPLETE", d5
    print("PASS: api persistence + verify")

    # 6. Worker cannot skip SEE — illegal transition protected
    t6 = engine.create_task("nope", success_criteria=["z"])
    try:
        engine._transition(t6, "COMPLETED", "cheat")
        print("FAIL: allowed illegal COMPLETED"); return 1
    except engine.TransitionError:
        print("PASS: illegal COMPLETED transition rejected")

    # 7. Tools registered
    from argus.tools import see_tools
    names = {t.name for t in see_tools.tools()}
    assert "see_start_task" in names and "see_request_verify" in names
    print("PASS: see_tools registered")

    print("\nALL SEE TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
