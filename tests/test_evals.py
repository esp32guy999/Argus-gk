"""Contract test for the eval suite (evals/harness.py + evals/run.py grading).

Offline + deterministic: fake tools, fake embedder, no registry build, no model.
Runnable standalone:  python tests/test_evals.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    import yaml
    from argus.registry import Registry, Tool
    from argus.semantic import SemanticSelector
    from evals.harness import CallLog, wrap_registry, toolset_cost, SAFE_LIVE
    from evals.run import grade_live, grade_selection

    # 1. tasks.yaml is well-formed: unique ids, every task graded somehow
    tasks = yaml.safe_load(open(os.path.join(os.path.dirname(__file__),
                                             os.pardir, "evals", "tasks.yaml")))
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"
    for t in tasks:
        assert t.get("prompt"), f"{t['id']}: no prompt"
        assert any(k in t for k in ("expect_call", "expect_any", "forbid_mutating",
                                    "answer_regex", "answer_contains", "needs",
                                    "needs_any")), f"{t['id']}: ungradeable"
    assert len(tasks) >= 20, "suite shrank below 20 tasks"
    print(f"PASS: tasks.yaml well-formed ({len(tasks)} tasks)")

    # 2. dry-run wrapping: unsafe recorded+canned, safe executes real fn
    hits = []
    reg = Registry()
    reg.add(Tool("get_time", "clock", ["time"], lambda: hits.append(1) or "12:00"))
    reg.add(Tool("HassTurnOn", "switch on", ["home"], lambda name: hits.append(2) or "REAL"))
    assert "get_time" in SAFE_LIVE and "HassTurnOn" not in SAFE_LIVE
    log = CallLog()
    wrap_registry(reg, log)
    tools = {t.name: t for t in reg.all()}
    assert tools["get_time"].func() == "12:00" and hits == [1]
    out = tools["HassTurnOn"].func(name="workshop light")
    assert out.get("dryrun") and hits == [1], "mutating tool executed for real!"
    assert log.called("HassTurnOn") and log.args_contain("HassTurnOn", "workshop")
    print("PASS: dry-run harness records + cans mutations, passes safe reads through")

    # 3. grading
    g = grade_live({"expect_call": {"tool": "HassTurnOn", "args_contains": "workshop"},
                    "answer_contains": "done"}, log, "All done.")
    assert g["success"] is True
    g = grade_live({"forbid_mutating": True}, log, "hi")
    assert g["success"] is False, "dry call should count as a mutation"
    print("PASS: graders judge call log + answer correctly")

    # 4. selection grading + always-pinning
    def fake_embed(texts):
        return [[1.0 if "light" in s.lower() else 0.0,
                 1.0 if "memory" in s.lower() else 0.0] for s in texts]
    mem = Tool("lookup_memory", "memory recall", ["memory"], lambda q: "")
    lit = Tool("turn_on", "light switch", ["home"], lambda: "")
    other = Tool("misc", "unrelated", [], lambda: "")
    sel = SemanticSelector([mem, lit, other], embed_fn=fake_embed, top_k=1,
                           always=("lookup_memory",))
    picked = sel.select("turn on the light")
    names = [t.name for t in picked]
    assert "lookup_memory" in names and "turn_on" in names, names
    gs = grade_selection({"needs": ["turn_on"]}, picked, [t.name for t in sel.rank("light")])
    assert gs["selection_hit"] is True and gs["count"] == 2
    gs = grade_selection({"needs": ["misc"]}, picked, [t.name for t in sel.rank("light")])
    assert gs["selection_hit"] is False and gs["missed"]["misc"] is not None
    assert toolset_cost(picked)["count"] == 2
    print("PASS: always-pinning + selection grading with miss ranks")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
