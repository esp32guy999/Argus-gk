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
              "summary": "docker ps shows sonarr container Up 2 hours"},
    ))
    engine.apply_event(task, SeeEvent(
        type="EvidenceAdded", detail="port responds",
        data={"criterion": "port responds", "kind": "http",
              "summary": "curl HTTP 200 from http://glassgarden:8989/ping"},
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
    api.add_evidence(t5.id, "a", "unit test observed criterion a passed with log", kind="test")
    api.add_evidence(t5.id, "b", "unit test observed criterion b passed with log", kind="test")
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
    assert "see_resume" in names
    print("PASS: see_tools registered")

    # 8. Weak evidence rejected (S4)
    t8 = engine.create_task("weak", success_criteria=["port up"])
    engine.accept_plan(t8)
    engine.apply_event(t8, SeeEvent(
        type="CheckpointReached", detail="x", data={"checklist_item": "port up"}))
    # ensure checklist item matches — plan uses success criteria as checklist
    if "port up" not in t8.completed_items:
        t8.completed_items.append("port up")
    engine.apply_event(t8, SeeEvent(
        type="EvidenceAdded", detail="port up",
        data={"criterion": "port up", "kind": "other", "summary": "ok"},
    ))
    d8 = engine.request_verify(t8)
    assert d8.action == "RETRY" and t8.current_state == "EXECUTING", (d8, t8.current_state)
    print("PASS: weak evidence 'ok' rejected")

    # 9. Strong evidence + resume after stall
    t9 = engine.create_task("resume-me", success_criteria=["done item"])
    engine.accept_plan(t9)
    engine.apply_event(t9, SeeEvent(
        type="CheckpointReached", detail="mid", data={"checklist_item": "done item"}))
    t9.last_progress_ts = time.time() - 999
    engine.tick(t9, now=time.time())
    assert t9.current_state == "STALLED"
    engine.supervisor_action(t9, "RESUME", reason="test")
    assert t9.current_state == "EXECUTING"
    engine.apply_event(t9, SeeEvent(
        type="EvidenceAdded", detail="done item",
        data={"criterion": "done item", "kind": "command",
              "summary": "curl exit 0; HTTP 200 from localhost:8989/ping"},
    ))
    d9 = engine.request_verify(t9)
    assert d9.action == "COMPLETE", d9
    print("PASS: resume from stall + strong evidence COMPLETE")

    # 10. api.resume + replay_events
    storage._STORE = None
    t10 = api.create("replay", success_criteria=["a"], conversation_id="c-resume")
    api.checkpoint(t10.id, "halfway", checklist_item="a")
    r10 = api.resume(t10.id)
    assert r10["ok"] and r10.get("last_checkpoint", {}).get("label") == "halfway"
    rep = api.replay_events(t10.id)
    assert rep["event_count"] >= 1 and any(
        e["type"] == "CheckpointReached" for e in rep["events"])
    print("PASS: resume + event replay")

    # 11. Unrelated evidence must not COMPLETE (jellyfin file ≠ sonarr criterion)
    t11 = engine.create_task(
        "Ensure Sonarr is running",
        success_criteria=["sonarr container running", "sonarr port responds"],
        checklist=["start sonarr", "check port"],
    )
    engine.accept_plan(t11)
    for item in t11.checklist:
        engine.apply_event(t11, SeeEvent(
            type="CheckpointReached", detail=item,
            data={"checklist_item": item},
        ))
    bogus = {
        "path": "/etc/jellyfin/config.xml",
        "contents": "service running",
    }
    for crit in t11.success_criteria:
        engine.apply_event(t11, SeeEvent(
            type="EvidenceAdded", detail=crit,
            data={
                "criterion": crit,
                "kind": "file",
                "summary": f"path={bogus['path']} contents={bogus['contents']}",
                "payload": str(bogus),
            },
        ))
    d11 = engine.request_verify(t11)
    assert d11.action != "COMPLETE", d11
    assert t11.current_state == "EXECUTING", t11.current_state
    assert "does not prove" in (d11.feedback or "").lower() or "jellyfin" in (d11.feedback or "").lower() \
        or "unrelated" in (d11.feedback or "").lower() or "sonarr" in (d11.feedback or "").lower(), d11.feedback
    print("PASS: unrelated jellyfin evidence rejected for sonarr criteria")

    print("\nALL SEE TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
