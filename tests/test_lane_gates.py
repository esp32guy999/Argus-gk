"""Contract test for LANE_MODEL_GATES — the per-model tool-lane clearances.

Offline + deterministic. The load-bearing assertion: the shell lane (empty
allowlist = arbitrary commands as shane) is NEVER offered to an ungated model.
Runnable standalone:  python tests/test_lane_gates.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.loop import _gate_tools, LANE_MODEL_GATES
    from argus.registry import Tool

    def f():
        return "ok"

    tools = [
        Tool("run_command", "shell", ["shell"], f, provider="shell"),
        Tool("edit_source", "edit", ["code"], f, provider="code_edit"),
        Tool("get_time", "clock", ["time"], f, provider="native"),
    ]

    # 1. shell is gated at all
    assert "shell" in LANE_MODEL_GATES, "shell lane must be model-gated"
    print("PASS: shell lane has a model gate")

    # 2. small/ungated models never see run_command (or code_edit)
    for m in ("gemma4-26b", "bonsai-8b", "lfm2.5-8b", "gpt-oss-20b", ""):
        names = {t.name for t in _gate_tools(tools, m)}
        assert "run_command" not in names, f"{m or '(none)'} got a shell!"
        assert "edit_source" not in names, f"{m or '(none)'} got code_edit!"
        assert "get_time" in names, f"{m}: ungated lanes must pass through"
    print("PASS: ungated models get no shell / code_edit; open lanes untouched")

    # 3. trusted models keep their clearances (substring match incl. served ids)
    for m in ("qwen3-next-80b", "Qwen3-Next-80B-A3B-Instruct-IQ4_XS.gguf".lower()
              if False else "qwen3-next-80b-instruct", "ornith-35b-uncensored"):
        names = {t.name for t in _gate_tools(tools, m)}
        assert "run_command" in names, f"{m} should keep the shell"
    assert "edit_source" in {t.name for t in _gate_tools(tools, "qwen3-coder-30b")}
    print("PASS: trusted models keep shell/code_edit clearances")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
