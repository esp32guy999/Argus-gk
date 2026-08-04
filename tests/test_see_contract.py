"""SEE worker step response contract (STP-1.1) — offline.

python tests/test_see_contract.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARGUS_DB"] = os.path.join(tempfile.mkdtemp(), "see-contract.db")


def main() -> int:
    import argus.storage as storage
    storage._STORE = None
    from argus.see import api, engine
    from argus.see.contract import (
        ContractError,
        assert_step_result,
        no_op_step,
        parse_worker_step,
    )
    from argus.see.models import PROTOCOL_ID, SeeEvent

    # --- pure parse ---
    try:
        parse_worker_step(None)
        print("FAIL: null accepted"); return 1
    except ContractError as e:
        assert "empty" in str(e).lower() or "null" in str(e).lower()
    try:
        parse_worker_step("")
        print("FAIL: blank accepted"); return 1
    except ContractError:
        pass
    try:
        parse_worker_step({})
        print("FAIL: empty object accepted"); return 1
    except ContractError:
        pass
    try:
        parse_worker_step({"reason": "x"})  # no action
        print("FAIL: missing action accepted"); return 1
    except ContractError as e:
        assert "action" in str(e).lower()
    print("PASS: empty/null/missing action rejected")

    step = parse_worker_step(no_op_step("No changes required."))
    assert step.action == "NO_OP" and step.is_no_op
    assert step.protocol == PROTOCOL_ID
    print("PASS: NO_OP step parses")

    step = parse_worker_step({
        "status": "COMPLETE",
        "action": "NO_OP",
        "reason": "Nothing to do.",
        "confidence": 1.0,
        "evidence": [],
    })
    assert step.action == "NO_OP"
    print("PASS: status=COMPLETE + action=NO_OP → step NO_OP")

    step = parse_worker_step({
        "decision": "CONTINUE",
        "explanation": "Nothing to do.",
        "confidence": 1.0,
        "evidence": [],
    })
    assert step.action == "CONTINUE" and "Nothing" in step.message_text()
    step = parse_worker_step({
        "action": "NO_OP",
        "message": "Configuration already satisfies requirements.",
    })
    assert step.message.startswith("Configuration")
    print("PASS: decision/explanation/message aliases")

    step = assert_step_result('{"action":"RETRY","code":"TEMPORARY_FAILURE"}')
    assert step.action == "RETRY"
    print("PASS: JSON string + assert_step_result")

    # --- engine apply ---
    task = engine.create_task("Health ok", success_criteria=["service up"])
    engine.accept_plan(task)
    assert task.current_state == "EXECUTING"

    prog_before = task.last_progress_ts
    act_before = task.last_activity_ts
    import time as _t
    _t.sleep(0.02)
    dec = engine.apply_worker_step(task, no_op_step(
        "Configuration already satisfies requirements."
    ))
    assert dec.action == "NO_OP" and dec.code == "NO_OP", dec
    assert task.current_state == "EXECUTING"
    assert task.consecutive_no_ops == 1
    assert task.last_progress_ts == prog_before, "NO_OP must not update progress"
    assert task.last_activity_ts >= act_before
    assert any(e.type == "WorkerStepReported" and e.data.get("step_id")
               for e in task.event_log)
    assert dec.details.get("consecutive_no_ops") == 1
    print("PASS: NO_OP activity-only, step_id, consecutive count")

    # Accumulation: stall after threshold
    engine.NO_OP_STALL_AT = 3
    engine.NO_OP_REVIEW_AT = 2
    task.consecutive_no_ops = 0
    for i in range(2):
        d = engine.apply_worker_step(task, no_op_step(f"still ok {i}"))
        assert d.action == "NO_OP", d
    assert task.consecutive_no_ops == 2
    assert engine.apply_worker_step(task, no_op_step("third")).action == "STALL"
    assert task.current_state == "STALLED"
    print("PASS: NO_OP accumulation → STALL")

    # Reset threshold for rest of suite
    engine.NO_OP_STALL_AT = 11
    engine.NO_OP_REVIEW_AT = 6

    t_mal = engine.create_task("mal", success_criteria=["x"])
    engine.accept_plan(t_mal)
    dec = engine.apply_worker_step(t_mal, None)
    assert dec.action == "RETRY" and dec.code == "MALFORMED_STEP", dec
    print("PASS: null step → RETRY MALFORMED_STEP (not success)")

    # Progress event resets NO_OP streak
    t_p = engine.create_task("prog", success_criteria=["x"])
    engine.accept_plan(t_p)
    engine.apply_worker_step(t_p, no_op_step())
    engine.apply_worker_step(t_p, no_op_step())
    assert t_p.consecutive_no_ops == 2
    engine.apply_event(t_p, SeeEvent(
        type="CheckpointReached", detail="moved",
        data={"checklist_item": "x"},
    ))
    assert t_p.consecutive_no_ops == 0
    print("PASS: real progress resets consecutive_no_ops")

    # COMPLETE without evidence still VERIFY_FAILED
    dec = engine.apply_worker_step(task, {
        "action": "COMPLETE",
        "reason": "I think we're done",
    })
    assert dec.action == "VERIFY_FAILED", dec
    print("PASS: worker COMPLETE without evidence → VERIFY_FAILED")

    # evidence on step then COMPLETE (mark checklist via checkpoint first)
    t2 = engine.create_task(
        "Port up",
        success_criteria=["port responds"],
        checklist=["port responds"],
        required_evidence=["port responds"],
    )
    engine.accept_plan(t2)
    engine.apply_event(t2, SeeEvent(
        type="CheckpointReached", detail="probed",
        data={"checklist_item": "port responds"},
    ))
    dec = engine.apply_worker_step(t2, {
        "action": "COMPLETE",
        "evidence": [{
            "criterion": "port responds",
            "summary": "curl HTTP 200 from http://127.0.0.1:8210/ — port responds",
            "kind": "http",
            "source": "tool:curl",
            "command": "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8210/",
        }],
    })
    assert dec.action == "COMPLETE" and t2.current_state == "COMPLETED", (dec, t2.current_state)
    print("PASS: step COMPLETE with tool evidence → COMPLETED")

    # --- api + tool surface ---
    t3 = api.create("noop check", success_criteria=["x"], start=True)
    dec = api.report_step(t3.id, no_op_step("already healthy"))
    assert dec.action == "NO_OP"
    brief = api.worker_brief(t3.id)
    assert "Response contract" in brief and "NO_OP" in brief
    print("PASS: api.report_step + worker_brief documents contract")

    from argus.tools import see_tools
    names = {t.name for t in see_tools.tools()}
    assert "see_report_step" in names
    out = see_tools.see_report_step(
        t3.id, "NO_OP", reason="still nothing", confidence=1.0,
    )
    assert out.get("no_op") is True and out.get("action") == "NO_OP"
    print("PASS: see_report_step tool")

    print("\nALL SEE CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
