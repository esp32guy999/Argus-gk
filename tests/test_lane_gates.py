"""Contract test for the per-model tool-lane clearances (argus/loop.py).

Covers LANE_MODEL_GATES (the in-code seed), LANE_MODEL_DENY (hard deny that beats
substring allows — keeps Loki out), and the runtime grants file written by the
permissions widget (config/lane_grants.json → effective_gates()).

Offline + deterministic. The load-bearing assertion: the shell lane (empty allowlist
= arbitrary commands as shane) is NEVER offered to a model that isn't cleared, and a
DENIED model can't hold it even via a hand-edited grants file.
Runnable standalone:  python tests/test_lane_gates.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, tempfile, shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import loop
    from argus.loop import _gate_tools, LANE_MODEL_GATES, LANE_MODEL_DENY
    from argus.registry import Tool

    def f():
        return "ok"

    tools = [
        Tool("run_command", "shell", ["shell"], f, provider="shell"),
        Tool("edit_source", "edit", ["code"], f, provider="code_edit"),
        Tool("get_time", "clock", ["time"], f, provider="native"),
    ]

    def offered(m):
        return {t.name for t in _gate_tools(tools, m)}

    # Hermetic: ignore any live config/lane_grants.json (the permissions widget writes
    # one) so these assertions test the in-code SEED, not the operator's current grants.
    import tempfile
    _op, _oc = loop._LANE_GRANTS_PATH, loop._grants_cache
    loop._LANE_GRANTS_PATH = os.path.join(tempfile.gettempdir(), "argus_no_such_grants.json")
    loop._grants_cache = (0.0, {})

    # 1. shell is gated at all
    assert "shell" in LANE_MODEL_GATES, "shell lane must be model-gated"
    print("PASS: shell lane has a model gate")

    # 2. small/ungated models never see run_command (or code_edit)
    for m in ("gemma4-26b", "bonsai-8b", "lfm2.5-8b", ""):
        n = offered(m)
        assert "run_command" not in n, f"{m or '(none)'} got a shell!"
        assert "edit_source" not in n, f"{m or '(none)'} got code_edit!"
        assert "get_time" in n, f"{m}: ungated lanes must pass through"
    print("PASS: ungated models get no shell / code_edit; open lanes untouched")

    # 3. trusted (seed-cleared) models keep their clearances
    for m in ("qwen3-next-80b", "qwen3-next-80b-instruct", "gpt-oss-20b"):
        assert "run_command" in offered(m), f"{m} should keep the shell"
    assert "edit_source" in offered("qwen3-coder-30b")
    assert offered("ornith-35b") >= {"run_command", "edit_source"}, "official ornith-35b keeps both"
    print("PASS: trusted models keep shell/code_edit clearances")

    # 4. LANE_MODEL_DENY — Loki is refused BOTH lanes even though its name embeds the
    #    cleared 'ornith-35b' substring. Deny beats allow.
    assert "ornith-35b-uncensored" in LANE_MODEL_DENY
    loki = offered("ornith-35b-uncensored")
    assert "run_command" not in loki and "edit_source" not in loki, "Loki must be denied all gated lanes"
    assert "get_time" in loki, "denied model still gets open lanes"
    print("PASS: LANE_MODEL_DENY locks Loki out (deny beats substring allow)")

    # 5. runtime grants file (permissions widget) overrides the seed, hot.
    tmp = tempfile.mkdtemp()
    orig_path, orig_cache = loop._LANE_GRANTS_PATH, loop._grants_cache
    loop._LANE_GRANTS_PATH = os.path.join(tmp, "lane_grants.json")
    loop._grants_cache = (0.0, {})
    try:
        # grant gemma shell; TRY to sneak Loki full perms; leave code_edit empty
        written = loop.save_lane_grants({
            "shell": ["gemma4-26b", "ornith-35b-uncensored"],
            "code_edit": [],
        })
        assert "ornith-35b-uncensored" not in written["shell"], "Loki must be stripped from writes"
        assert "run_command" in offered("gemma4-26b"), "grant took effect live"
        assert "edit_source" not in offered("gemma4-26b"), "only granted lane opens"
        # an empty lane in the file closes it for EVERYONE (even the seed-trusted official)
        assert "edit_source" not in offered("ornith-35b"), "empty code_edit grant closes it for all"
        # Loki stays denied no matter what the file says
        assert "run_command" not in offered("ornith-35b-uncensored"), "deny still wins over the file"
        print("PASS: grants file overrides seed live; Loki stripped; empty lane closes for all")
    finally:
        loop._LANE_GRANTS_PATH, loop._grants_cache = orig_path, orig_cache
        shutil.rmtree(tmp, ignore_errors=True)

    loop._LANE_GRANTS_PATH, loop._grants_cache = _op, _oc   # restore the live path
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
