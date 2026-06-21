"""Contract test for the self-observer (argus/observability.py).

Proves the consumer actually FIRES on a metric breach (so the cornerstone can't rot
back into emitted-but-unwatched theater) and respects the cooldown. Drives the real
metrics + a fake notifier; the registry is fresh per test process. Offline.
    python tests/test_observability.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import metrics, observability

    calls = []
    obs = observability.SelfObserver(lambda t, m: calls.append((t, m)),
                                     cooldown=1000, cc_cost_alert=5.0)
    t = 0.0

    # 1. baseline pass fires nothing
    assert obs.check(now=t) == [], "first pass should only prime baseline"
    print("PASS: baseline pass is silent")

    # 2. an agent error spike fires once
    for _ in range(4):
        metrics.AGENT_TURNS.labels("error").inc()
    metrics.AGENT_TURNS.labels("ok").inc()
    t += 1
    fired = obs.check(now=t)
    assert "agent_errors" in fired, f"error spike should fire: {fired}"
    assert calls and "error rate high" in calls[-1][1], calls
    print("PASS: agent error spike fires an alert")

    # 3. cooldown — another spike immediately does NOT re-fire
    for _ in range(4):
        metrics.AGENT_TURNS.labels("error").inc()
    t += 1
    fired = obs.check(now=t)
    assert "agent_errors" not in fired, "cooldown should suppress repeat"
    print("PASS: cooldown suppresses repeat alert")

    # 4. loop detection fires
    metrics.LOOP_DETECTED.inc()
    t += 1
    assert "loops" in obs.check(now=t), "loop_detected should fire"
    print("PASS: loop-detected fires")

    # 5. CC cost threshold fires when spend crosses the step
    metrics.CC_COST.inc(6.0)
    t += 1
    assert "cc_cost" in obs.check(now=t), "cc cost over step should fire"
    assert any("spend passed" in m for _, m in calls), calls
    print("PASS: CC cost threshold fires")

    # 6. quiet pass after everything settles fires nothing new
    t += 1
    assert obs.check(now=t) == [], "no new breaches -> silent"
    print("PASS: quiet pass is silent")

    print("\nALL OBSERVABILITY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
