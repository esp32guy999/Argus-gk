"""Contract test for the metrics module (argus/metrics.py).

Asserts the sensor set exists and is labelable — so a refactor can't silently drop a
metric (which would re-create a blind spot). Offline. python tests/test_metrics.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import metrics

    required = ["TOOL_CALLS", "TOOL_LATENCY", "TOOL_ERRORS", "TOOL_RETRY",
                "AGENT_TURNS", "LOOP_DETECTED", "NO_PROGRESS", "TASK_DURATION",
                "TOOLS_SELECTED", "CC_TURNS", "CC_COST", "CC_DURATION"]
    missing = [m for m in required if not hasattr(metrics, m)]
    assert not missing, f"missing metrics: {missing}"
    print(f"PASS: all {len(required)} sensors defined")

    # labeled sensors must accept their labels (catches arity drift)
    metrics.TOOL_CALLS.labels("prov", "tool", "ok")
    metrics.TOOL_ERRORS.labels("prov", "tool", "ValueError")
    metrics.AGENT_TURNS.labels("ok")
    metrics.CC_TURNS.labels("error")
    print("PASS: labeled sensors accept their labels")

    assert callable(metrics.serve), "metrics.serve missing"
    print("PASS: metrics.serve present")
    print("\nALL METRICS TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
