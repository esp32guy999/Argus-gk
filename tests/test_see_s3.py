"""SEE S3 — planner structured JSON + /task slash handler.

Standalone: python tests/test_see_s3.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ARGUS_DB"] = os.path.join(tempfile.mkdtemp(), "see-s3.db")


def main() -> int:
    import argus.storage as storage
    storage._STORE = None
    from argus.see import planner, slash, api

    # 1. Freeform goal → default criteria
    p = planner.plan_from_intent("Verify Sonarr is healthy on glassgarden")
    assert p.goal and p.success_criteria, p
    assert not p.validate(), p.validate()
    print("PASS: freeform plan has goal + criteria")

    # 2. Sectioned plan
    p2 = planner.plan_from_intent("""
Goal: Deploy homepage
criteria:
- container running
- port 3000 responds
checklist:
- write compose
- up -d
- curl health
""")
    assert "Deploy homepage" in p2.goal
    assert any("3000" in c for c in p2.success_criteria), p2
    assert any("compose" in c for c in p2.checklist), p2
    print("PASS: sectioned plan parses criteria + checklist")

    # 3. JSON plan
    p3 = planner.plan_from_intent(
        '{"goal":"Fix VPN","success_criteria":["egress via PIA","anvil reaches ABB"],"importance":80}'
    )
    assert p3.goal == "Fix VPN" and p3.importance == 80
    assert len(p3.success_criteria) == 2
    print("PASS: JSON plan")

    # 4. /task help
    r = slash.handle_task_command("/task -help")
    assert r["ok"] and "SEE" in r["markdown"]
    print("PASS: /task help")

    # 5. /task start + status + verify fail + evidence path via api
    r = slash.handle_task_command(
        "/task Confirm Sonarr\ncriteria:\n- container running\n- port responds",
        conversation_id="s3-conv",
    )
    assert r["ok"] and r.get("task_id"), r
    tid = r["task_id"]
    st = slash.handle_task_command("/task status", conversation_id="s3-conv")
    assert st["ok"] and st["task_id"] == tid and st["state"] == "EXECUTING"
    print("PASS: /task start + status")

    v = slash.handle_task_command("/task verify", conversation_id="s3-conv")
    assert v.get("action") == "RETRY" or "missing" in (v.get("markdown") or "").lower()
    print("PASS: /task verify without evidence fails closed")

    api.add_evidence(tid, "container running",
                     "docker ps shows sonarr container Up", kind="docker")
    api.add_evidence(tid, "port responds",
                     "curl HTTP 200 from glassgarden:8989/ping", kind="http")
    api.checkpoint(tid, "all checks", checklist_item="container running")
    # mark second checklist if different
    task = api.get(tid)
    for item in task.checklist:
        if item not in task.completed_items:
            api.checkpoint(tid, f"done {item}", checklist_item=item)
    v2 = slash.handle_task_command("/task verify", conversation_id="s3-conv")
    assert v2.get("action") == "COMPLETE" or "COMPLETE" in (v2.get("markdown") or ""), v2
    print("PASS: /task verify succeeds with evidence")

    # 6. list
    lst = slash.handle_task_command("/task list", conversation_id="s3-conv")
    assert lst["ok"] and tid in lst["markdown"]
    print("PASS: /task list")

    print("\nALL SEE S3 TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
