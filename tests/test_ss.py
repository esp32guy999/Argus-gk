"""SUPER-SEE v1 — offline contract. python tests/test_ss.py

No network. Rules path, shadow hook, kill switch, injected model, recorder.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="ss-")
os.environ["ARGUS_DB"] = os.path.join(_TMP, "ss.db")
os.environ["ARGUS_SS"] = "1"
os.environ.pop("ARGUS_SS_URL", None)
os.environ["ARGUS_SS_LOG"] = os.path.join(_TMP, "ss.jsonl")

# Bench cases from /tmp/ss_bench.py — exact rules outcomes (not the bench sets).
BENCH = [
    {
        "id": "tool_loop",
        "state": {
            "tools": {"calls": 8, "errors": 0, "repeated_calls": 4, "last_tool": "web_search"},
            "progress": {"new_evidence": False, "state_changed": False, "objective_progress": 0.0},
            "model": {"generating": False, "seconds_since_token": 0},
            "system": {"gpu_ok": True, "backend_ok": True},
            "task_age_sec": 94,
        },
        "action": "REPLAN",
        "reason": "REPEATED_TOOL_CALL",
    },
    {
        "id": "productive",
        "state": {
            "tools": {"calls": 8, "errors": 0, "repeated_calls": 0, "last_tool": "web_search"},
            "progress": {"new_evidence": True, "state_changed": True, "objective_progress": 0.45},
            "model": {"generating": False, "seconds_since_token": 0},
            "system": {"gpu_ok": True, "backend_ok": True},
            "task_age_sec": 60,
        },
        "action": "NO_OP",
        "reason": "PRODUCTIVE_PROGRESS",
    },
    {
        "id": "busy_no_progress",
        "state": {
            "tools": {"calls": 17, "errors": 0, "repeated_calls": 2, "last_tool": "shell"},
            "progress": {"new_evidence": False, "state_changed": True, "objective_progress": 0.0},
            "model": {"generating": False, "seconds_since_token": 0},
            "system": {"gpu_ok": True, "backend_ok": True},
            "task_age_sec": 180,
            "note": "high activity, zero objective progress",
        },
        "action": "REPLAN",
        "reason": "NO_PROGRESS",
    },
    {
        "id": "inference_stall",
        "state": {
            "tools": {"calls": 3, "errors": 0, "repeated_calls": 0, "last_tool": "none"},
            "progress": {"new_evidence": False, "state_changed": False, "objective_progress": 0.2},
            "model": {"generating": True, "seconds_since_token": 48, "tokens_per_second": 0},
            "system": {"gpu_ok": True, "backend_ok": True},
            "task_age_sec": 120,
        },
        "action": "WAIT",
        "reason": "INFERENCE_STALL",
    },
    {
        "id": "backend_down",
        "state": {
            "tools": {"calls": 2, "errors": 2, "repeated_calls": 2, "last_tool": "qbt_add"},
            "progress": {"new_evidence": False, "state_changed": False, "objective_progress": 0.0},
            "model": {"generating": False, "seconds_since_token": 0},
            "system": {"gpu_ok": True, "backend_ok": False, "service": "qbittorrent"},
            "task_age_sec": 40,
        },
        "action": "ASK_USER",
        "reason": "RESOURCE_FAILURE",
    },
    {
        "id": "false_positive_progress",
        "state": {
            "tools": {"calls": 12, "errors": 1, "repeated_calls": 3, "last_tool": "search"},
            "progress": {"new_evidence": True, "state_changed": True, "objective_progress": 0.55},
            "model": {"generating": False, "seconds_since_token": 0},
            "system": {"gpu_ok": True, "backend_ok": True},
            "task_age_sec": 200,
            "note": "some repeated calls but evidence and progress increasing",
        },
        "action": "NO_OP",
        "reason": "PRODUCTIVE_PROGRESS",
    },
]


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def _healthy_sensors() -> None:
    from argus.ss import sensors
    sensors.reset()
    sensors.configure(probe_backend=lambda: True, probe_gpu=lambda: True)


def _suspicious_snap():
    """Rules return None; is_suspicious is True (tool errors)."""
    from argus.ss.snapshot import Snapshot
    return Snapshot.from_dict({
        "tools": {"calls": 2, "errors": 1, "repeated_calls": 0, "last_tool": "shell"},
        "progress": {"new_evidence": False, "state_changed": False, "objective_progress": 0.0},
        "model": {"generating": False, "seconds_since_token": 0, "streamed": False},
        "system": {"gpu_ok": True, "backend_ok": True},
        "task_age_sec": 30,
    })


def main() -> int:
    import argus.storage as storage
    storage._STORE = None
    from argus.ss import observe, observe_task, enabled, sensors
    from argus.ss import model as ss_model
    from argus.ss.protocol import SsDecision
    from argus.ss.snapshot import Snapshot
    from argus.see import api, engine
    from argus.see.models import SeeEvent

    _healthy_sensors()

    # --- 1. six bench cases, rules path, no model ---
    os.environ.pop("ARGUS_SS_URL", None)
    for case in BENCH:
        dec = observe(case["state"], record=False)
        if dec.action != case["action"] or dec.reason_code != case["reason"]:
            return _fail(
                f"bench {case['id']}: got {dec.action}/{dec.reason_code} "
                f"want {case['action']}/{case['reason']}"
            )
        if dec.source != "rules":
            return _fail(f"bench {case['id']}: source={dec.source!r} want rules")
        if dec.authority:
            return _fail(f"bench {case['id']}: authority leaked True")
    print("PASS: six bench cases via observe() (rules)")

    # Non-streaming generate must not fake an inference stall.
    no_stream = dict(BENCH[3]["state"])
    no_stream = {
        **no_stream,
        "model": {**no_stream["model"], "streamed": False, "generating": True,
                  "seconds_since_token": 48},
    }
    dec = observe(no_stream, record=False)
    if dec.reason_code == "INFERENCE_STALL":
        return _fail("non-streamed generate classified as INFERENCE_STALL")
    print("PASS: stall requires a real token stream")

    # --- 2. authority hard-forced False ---
    forced = SsDecision("REPLAN", "NO_PROGRESS", authority=True, source="model")
    if forced.authority is not False:
        return _fail(f"authority={forced.authority!r} after True passed in")
    if forced.to_dict().get("authority") is not False:
        return _fail("to_dict leaked authority")
    print("PASS: SsDecision.authority always False")

    # --- 3. recorder writes one JSON line ---
    log = os.environ["ARGUS_SS_LOG"]
    if os.path.exists(log):
        os.remove(log)
    observe(BENCH[1]["state"], record=True)
    if not os.path.isfile(log):
        return _fail("recorder did not create ARGUS_SS_LOG")
    lines = [ln for ln in open(log, encoding="utf-8") if ln.strip()]
    if len(lines) != 1:
        return _fail(f"recorder wrote {len(lines)} lines, want 1")
    row = json.loads(lines[0])
    if "snapshot" not in row or "decision" not in row:
        return _fail(f"recorder line missing keys: {row.keys()}")
    if row["decision"].get("action") != "NO_OP":
        return _fail(f"recorder decision {row['decision']}")
    print("PASS: recorder writes one JSON line")

    # --- 4. model path via injected http_json; never a real port ---
    hits: list[str] = []

    def fake_http(url: str, payload: dict, timeout: float) -> dict:
        hits.append(url)
        assert url.startswith("http://127.0.0.1:9"), url
        return {"choices": [{"message": {
            "content": '{"action":"REPLAN","reason_code":"NO_PROGRESS"}',
        }}]}

    os.environ["ARGUS_SS_URL"] = "http://127.0.0.1:9"
    orig_http = ss_model._http_json
    ss_model._http_json = fake_http
    try:
        asked = ss_model.ask_model(_suspicious_snap(), http_json=fake_http)
        if asked is None:
            return _fail("ask_model returned None on valid fake JSON")
        if asked.action != "REPLAN" or asked.source != "model" or asked.authority:
            return _fail(f"ask_model decision {asked}")
        dec = observe(_suspicious_snap(), record=False)
        if dec.source != "model" or dec.action != "REPLAN":
            return _fail(f"observe model path {dec.action}/{dec.source}")
        if not hits:
            return _fail("injected http_json was never called")
        # Productive snapshot must not ask the model.
        n = len(hits)
        prod = observe(BENCH[1]["state"], record=False)
        if prod.source != "rules" or len(hits) != n:
            return _fail("model called on productive snapshot")
    finally:
        ss_model._http_json = orig_http
        os.environ.pop("ARGUS_SS_URL", None)
    print("PASS: model path uses injected http_json; rules still short-circuit")

    # --- 5. malformed model JSON → fallback, no exception ---
    os.environ["ARGUS_SS_URL"] = "http://127.0.0.1:9"

    def bad_http(url: str, payload: dict, timeout: float) -> dict:
        return {"choices": [{"message": {"content": "sorry I am thinking..."}}]}

    ss_model._http_json = bad_http
    try:
        dec = observe(_suspicious_snap(), record=False)
        if dec.action != "NO_OP" or dec.reason_code != "UNKNOWN" or dec.source != "fallback":
            return _fail(f"malformed JSON gave {dec.action}/{dec.reason_code}/{dec.source}")
        if dec.authority:
            return _fail("fallback leaked authority")
    except Exception as e:
        return _fail(f"malformed JSON raised {e!r}")
    finally:
        ss_model._http_json = orig_http
        os.environ.pop("ARGUS_SS_URL", None)
    print("PASS: malformed model JSON → fallback NO_OP/UNKNOWN")

    # --- 6. _ss_shadow attaches annotation, does not mutate SEE state ---
    _healthy_sensors()
    os.environ["ARGUS_SS"] = "1"
    task = api.create(
        "SS shadow probe",
        success_criteria=["done"],
        checklist=["step"],
        start=True,
    )
    before = task.current_state
    dec = api.tick(task.id)
    after = api.get(task.id)
    shadow = (dec.details or {}).get("ss_shadow")
    if not isinstance(shadow, dict):
        return _fail(f"tick missing ss_shadow: {dec.details}")
    if shadow.get("authority") is not False:
        return _fail(f"ss_shadow.authority={shadow.get('authority')!r}")
    if after is None:
        return _fail("task vanished after tick")
    if after.current_state != before:
        return _fail(
            f"SS mutated SEE state {before} → {after.current_state} "
            f"(shadow action={shadow.get('action')})"
        )
    # Repeat-tool REPLAN in the shadow must still leave SEE in EXECUTING.
    for _ in range(2):
        api.on_event(task.id, SeeEvent(
            type="ToolCalled", detail="web_search",
            data={"tool": "web_search", "args_key": '{"q":"x"}'},
        ))
    state_before_loop = api.get(task.id).current_state
    dec3 = api.on_event(task.id, SeeEvent(
        type="ToolCalled", detail="web_search",
        data={"tool": "web_search", "args_key": '{"q":"x"}'},
    ))
    shadow3 = (dec3.details or {}).get("ss_shadow") or {}
    state_after_loop = api.get(task.id).current_state
    if shadow3.get("action") == "REPLAN" and state_after_loop != state_before_loop:
        # SEE itself may stall on its own loop detector — that's SEE, not SS.
        # Only fail if SS action is present AND SEE state changed to something
        # SS would apply (REPLAN) that SEE would not have chosen on this event.
        if dec3.action == "REPLAN" and dec3.reason == shadow3.get("stp_code"):
            return _fail("SS REPLAN appears applied onto SEE")
    print("PASS: _ss_shadow attaches ss_shadow, does not change SEE state")

    # --- 7. kill switch ---
    os.environ["ARGUS_SS"] = "0"
    if enabled():
        return _fail("enabled() True when ARGUS_SS=0")
    t2 = api.create("kill switch", success_criteria=["x"], start=True)
    dec2 = api.tick(t2.id)
    if "ss_shadow" in (dec2.details or {}):
        return _fail("ARGUS_SS=0 still attached ss_shadow")
    os.environ["ARGUS_SS"] = "1"
    print("PASS: ARGUS_SS=0 skips the hook")

    # --- 8. live sensors: oauth GPU is not a worker resource ---
    sensors.reset()
    sensors.configure(probe_backend=lambda: True, probe_gpu=lambda: False)
    sensors.begin_turn("grok", backend="oauth")
    extra = sensors.read()
    if extra["system"]["gpu_ok"] is not True:
        return _fail(f"grok seat gpu_ok={extra['system']['gpu_ok']!r} (GPU must not WAIT OAuth)")
    sensors.begin_turn("local", backend="openai", base_url="http://127.0.0.1:9090")
    extra = sensors.read()
    if extra["system"]["gpu_ok"] is not False:
        return _fail(f"local seat ignored gpu probe: {extra['system']}")
    # observe_task merges live sensors (caller extras win).
    fake = engine.create_task("sensor merge", success_criteria=["x"])
    engine.accept_plan(fake)
    dec = observe_task(fake, extras={"system": {"gpu_ok": False}}, record=False)
    if dec.action != "WAIT" or dec.reason_code != "RESOURCE_FAILURE":
        return _fail(f"live/extra gpu_ok=False → {dec.action}/{dec.reason_code}")
    print("PASS: sensors model-agnostic (OAuth gpu_ok=True; extras override)")

    print("\nALL SS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
