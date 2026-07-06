"""Argus eval runner — the keep/discard arbiter for harness changes.

Two phases per task:
  selection — does registry.select(prompt) offer the tools the task needs,
              and what does the offered set cost in context tokens?
  live      — run the real loop against a local model with the dry-run harness
              (evals/harness.py) and grade the call log + final answer.

Usage (from repo root, venv python):
  .venv/bin/python evals/run.py --selection-only --selector semantic:8
  .venv/bin/python evals/run.py --selector all --model gemma4-26b
  .venv/bin/python evals/run.py --selector semantic:8+core --only light-on-workshop
  .venv/bin/python evals/run.py --compare evals/results/a.json evals/results/b.json

Selector configs:  all | semantic:<K> | semantic:<K>+core
(+core pins the prompt-referenced tools: lookup_memory, start_background_task)

Results land in evals/results/<selector>-<model>-<ts>.json. Scores are only
comparable across runs with an identical tasks.yaml (its sha1 is recorded).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from evals.harness import CallLog, wrap_registry, toolset_cost  # noqa: E402

CORE = ("lookup_memory", "start_background_task")


# ── selector configs ────────────────────────────────────────────────────
def apply_selector(reg, spec: str):
    """Mutate reg to the requested selector config. Returns a describing dict."""
    from argus.semantic import SemanticSelector
    if spec == "all":
        reg.semantic = None
        return {"selector": "all"}
    m = re.fullmatch(r"semantic:(\d+)(\+core)?", spec)
    if not m:
        raise SystemExit(f"bad --selector {spec!r} (use: all | semantic:K | semantic:K+core)")
    k, core = int(m.group(1)), bool(m.group(2))
    reg.semantic = SemanticSelector(reg.all(), top_k=k, always=CORE if core else ())
    return {"selector": spec, "top_k": k, "core": core}


# ── grading ─────────────────────────────────────────────────────────────
def grade_selection(task: dict, offered, ranked_names: list[str]) -> dict:
    names = {t.name for t in offered}
    out = {"offered": sorted(names), **toolset_cost(offered)}
    needs, needs_any = task.get("needs", []), task.get("needs_any", [])
    hit = all(n in names for n in needs) and (not needs_any or any(n in names for n in needs_any))
    missed = [n for n in needs if n not in names] + \
             ([] if not needs_any or any(n in names for n in needs_any) else needs_any)
    out["selection_hit"] = hit
    if missed and ranked_names:
        out["missed"] = {n: (ranked_names.index(n) + 1 if n in ranked_names else None)
                         for n in missed}   # rank tells us how close top-k was
    return out


def _call_matches(log: CallLog, ec: dict) -> bool:
    ok = bool(log.called(ec["tool"]))
    if ok and ec.get("args_contains"):
        ok = log.args_contain(ec["tool"], ec["args_contains"])
    return ok


def grade_live(task: dict, log: CallLog, answer: str) -> dict:
    checks: dict[str, bool] = {}
    ec = task.get("expect_call")
    if ec:
        checks["expect_call"] = _call_matches(log, ec)
    for i, ec in enumerate(task.get("expect_calls", [])):   # multi-step: ALL must appear
        checks[f"step{i + 1}:{ec['tool']}"] = _call_matches(log, ec)
    if task.get("expect_any"):
        checks["expect_any"] = any(log.called(t) for t in task["expect_any"])
    if task.get("forbid_mutating"):
        checks["no_mutation"] = not any(c["mode"] == "dry" for c in log.calls)
    if task.get("answer_regex"):
        checks["answer_regex"] = bool(re.search(task["answer_regex"], answer or "",
                                                re.IGNORECASE))
    if task.get("answer_contains"):
        checks["answer_contains"] = task["answer_contains"].lower() in (answer or "").lower()
    return {"checks": checks, "success": all(checks.values()) if checks else None,
            "tool_calls": len(log.calls),
            "called": [c["tool"] for c in log.calls]}


# ── main ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="evals/tasks.yaml")
    ap.add_argument("--selector", default="semantic:8")
    ap.add_argument("--model", default="gemma4-26b")
    ap.add_argument("--base-url", default="http://localhost:9090/v1")
    ap.add_argument("--selection-only", action="store_true")
    ap.add_argument("--no-anti-stall", action="store_true",
                    help="A/B: disable the driver's announce-nudge/summary fixes")
    ap.add_argument("--only", nargs="*", help="task ids to run")
    ap.add_argument("--compare", nargs=2, metavar="JSON", help="diff two result files")
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare)

    tasks = yaml.safe_load(open(args.tasks))
    suite_sha = hashlib.sha1(open(args.tasks, "rb").read()).hexdigest()[:10]
    if args.only:
        tasks = [t for t in tasks if t["id"] in set(args.only)]

    from argus.main import build_registry
    from argus import loop
    reg = build_registry()
    cfg = apply_selector(reg, args.selector)

    # validate task tool names against the real registry (typo guard)
    known = {t.name for t in reg.all()}
    for t in tasks:
        for n in t.get("needs", []) + t.get("needs_any", []) + t.get("expect_any", []):
            if n not in known:
                print(f"WARN: task {t['id']} references unknown tool {n!r}")

    log = CallLog()
    if not args.selection_only:
        wrap_registry(reg, log)

    results = []
    for t in tasks:
        row: dict = {"id": t["id"], "category": t.get("category", "")}
        offered = loop._gate_tools(reg.select(t["prompt"]), args.model)
        ranked_names = []
        if reg.semantic is not None:
            try:
                ranked_names = [x.name for x in reg.semantic.rank(t["prompt"])]
            except Exception:
                pass
        row["selection"] = grade_selection(t, offered, ranked_names)

        if not args.selection_only:
            log.reset()
            stall = {"loops": 0, "nudges": 0, "exhausted": False}

            def on_event(phase, detail, step, _s=stall):
                if phase == "loop":
                    _s["loops"] += 1
                elif phase == "nudge":
                    _s["nudges"] += 1
                elif phase == "exhausted":
                    _s["exhausted"] = True

            t0 = time.perf_counter()
            try:
                answer = loop.run(reg, t["prompt"], model_name=args.model,
                                  base_url=args.base_url, turn_budget=8,
                                  on_event=on_event,
                                  anti_stall=not args.no_anti_stall)
            except Exception as e:
                answer = f"(loop error: {e})"
            row["live"] = grade_live(t, log, answer)
            row["live"]["seconds"] = round(time.perf_counter() - t0, 1)
            row["live"]["answer"] = (answer or "")[:400]
            row["live"]["stall"] = stall
        results.append(row)
        _print_row(row)

    summary = summarize(results, cfg, args, suite_sha)
    print("\n== " + json.dumps(summary["aggregate"]))
    os.makedirs("evals/results", exist_ok=True)
    out = f"evals/results/{args.selector.replace(':', '')}-{args.model}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    json.dump(summary, open(out, "w"), indent=1)
    print(f"saved: {out}")
    return 0


def _print_row(row: dict):
    sel = row["selection"]
    live = row.get("live")
    mark = lambda v: "·" if v is None else ("✓" if v else "✗")
    line = (f"{row['id']:24} sel:{mark(sel['selection_hit'])} "
            f"n={sel['count']:<3} ~{sel['schema_tokens_est']:<5}tok")
    if live:
        line += f" live:{mark(live['success'])} calls={live['tool_calls']} {live['seconds']}s"
        bad = [k for k, v in live["checks"].items() if not v]
        if bad:
            line += "  FAILED:" + ",".join(bad)
    if sel.get("missed"):
        line += f"  missed={sel['missed']}"
    print(line)


def summarize(results, cfg, args, suite_sha) -> dict:
    n = len(results)
    sel_hits = sum(1 for r in results if r["selection"]["selection_hit"])
    agg = {
        "tasks": n,
        "selection_hit_rate": round(sel_hits / n, 3) if n else 0,
        "avg_tools_offered": round(sum(r["selection"]["count"] for r in results) / n, 1) if n else 0,
        "avg_schema_tokens": round(sum(r["selection"]["schema_tokens_est"] for r in results) / n) if n else 0,
    }
    lives = [r["live"] for r in results if r.get("live") and r["live"]["success"] is not None]
    if lives:
        agg["live_tasks"] = len(lives)
        agg["live_success_rate"] = round(sum(1 for l in lives if l["success"]) / len(lives), 3)
        agg["avg_tool_calls"] = round(sum(l["tool_calls"] for l in lives) / len(lives), 1)
        agg["avg_seconds"] = round(sum(l["seconds"] for l in lives) / len(lives), 1)
        agg["stalls_exhausted"] = sum(1 for l in lives if l.get("stall", {}).get("exhausted"))
        agg["stalls_looped"] = sum(l.get("stall", {}).get("loops", 0) for l in lives)
        agg["stalls_nudged"] = sum(l.get("stall", {}).get("nudges", 0) for l in lives)
    return {"config": cfg, "model": args.model, "suite_sha": suite_sha,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "aggregate": agg, "tasks": results}


def compare(a_path: str, b_path: str) -> int:
    a, b = json.load(open(a_path)), json.load(open(b_path))
    if a["suite_sha"] != b["suite_sha"]:
        print(f"WARN: different suites ({a['suite_sha']} vs {b['suite_sha']}) — not comparable")
    print(f"{'':26} {a['config']['selector']:>16} {b['config']['selector']:>16}")
    keys = sorted(set(a["aggregate"]) | set(b["aggregate"]))
    for k in keys:
        print(f"{k:26} {a['aggregate'].get(k, '—'):>16} {b['aggregate'].get(k, '—'):>16}")
    at = {t["id"]: t for t in a["tasks"]}
    for t in b["tasks"]:
        o = at.get(t["id"])
        if not o or not t.get("live") or not o.get("live"):
            continue
        if o["live"]["success"] != t["live"]["success"]:
            print(f"flip: {t['id']:24} {o['live']['success']} -> {t['live']['success']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
