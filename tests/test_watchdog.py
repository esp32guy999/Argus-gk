"""Watchdog contract test — a repeated identical tool call triggers intervention.

Uses Pydantic AI's FunctionModel (no real LLM) to FORCE a loop: the model always
emits the same (tool, args). Asserts the watchdog's loop-detection metric fires.
Runnable standalone:  python tests/test_watchdog.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.usage import UsageLimits

    from argus import watchdog, metrics

    # A model that ALWAYS calls the same tool with the same args -> a forced loop.
    def loop_model(messages, info):
        return ModelResponse(parts=[ToolCallPart(tool_name="noop", args={"x": "same"})])

    def noop(x: str) -> str:
        "A no-op tool."
        return "ok"

    agent = Agent(
        FunctionModel(loop_model),
        tools=[noop],
        capabilities=[watchdog.make_capability(repeat_limit=2)],
    )

    before = metrics.LOOP_DETECTED._value.get()
    try:
        agent.run_sync("go", usage_limits=UsageLimits(request_limit=5))
    except Exception:
        pass  # the forced loop ends via budget / retry exhaustion — that's expected
    after = metrics.LOOP_DETECTED._value.get()

    assert after > before, f"watchdog did not fire: loop_detected {before} -> {after}"
    print(f"PASS: watchdog intervened on repeated call (loop_detected {before} -> {after})")
    print("\nWATCHDOG CONTRACT TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
