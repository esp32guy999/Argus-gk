#!/usr/bin/env python3
"""Gemma observation harness — run a model through the REAL Argus loop on a task and
capture the full tool trace: the tools it was offered, each tool call (name + args),
each result, any ModelRetry/tool-error it got back (and whether it heeded it), and the
final answer. Grading vs ground truth is done separately (by a human/Claude verifier).

Faithful to production: same semantic tool selection + lane gating (_gate_tools reads
the live config/lane_grants.json) + soul overlay as loop.run().

Usage:
  python scripts/gemma_probe.py "what's the weather in Dahlonega?"
  python scripts/gemma_probe.py "add the movie Nefarious" --model gemma4-26b --budget 8
  python scripts/gemma_probe.py "..." --json      # machine-readable
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:9090/v1")


def probe(task: str, model_name: str = "gemma4-26b", budget: int = 8) -> dict:
    from argus.main import build_registry
    from argus import loop, watchdog
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    reg = build_registry()
    selected = loop._gate_tools(reg.select(task), model_name)
    agent = Agent(
        loop.make_model(model_name, BASE_URL),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=loop._system_prompt(model_name),
        capabilities=[watchdog.make_capability()],
    )
    t0, err, msgs = time.perf_counter(), None, []
    try:
        result = agent.run_sync(task, usage_limits=UsageLimits(request_limit=budget),
                                model_settings=loop._thinking_settings(False))
        final = result.output
        msgs = result.all_messages()
    except Exception as e:
        final, err = f"<EXCEPTION: {type(e).__name__}: {e}>", str(e)
    secs = round(time.perf_counter() - t0, 1)

    trace = []
    for msg in msgs:
        for part in getattr(msg, "parts", []):
            k = type(part).__name__
            if k == "ToolCallPart":
                trace.append({"kind": "call", "tool": part.tool_name, "args": part.args})
            elif k == "ToolReturnPart":
                trace.append({"kind": "result", "tool": part.tool_name,
                              "content": str(part.content)[:600]})
            elif k == "RetryPromptPart":   # a tool raised ModelRetry (honest error)
                trace.append({"kind": "retry", "tool": getattr(part, "tool_name", None),
                              "error": str(part.content)[:400]})
    return {"task": task, "model": model_name, "seconds": secs, "error": err,
            "offered": sorted(t.name for t in selected),
            "n_calls": sum(1 for t in trace if t["kind"] == "call"),
            "trace": trace, "final": final}


def _print(r: dict) -> None:
    print(f"\n=== TASK: {r['task']}")
    print(f"model={r['model']} | {r['seconds']}s | tool_calls={r['n_calls']} "
          f"| offered={len(r['offered'])}")
    print(f"offered: {', '.join(r['offered'])}")
    for s in r["trace"]:
        if s["kind"] == "call":
            print(f"  → CALL {s['tool']}({json.dumps(s['args'], default=str)[:220]})")
        elif s["kind"] == "result":
            print(f"    ↳ {s['content'][:220]}")
        elif s["kind"] == "retry":
            print(f"    ⚠ RETRY/{s['tool']}: {s['error'][:220]}")
    if r["error"]:
        print(f"  !! ERROR: {r['error']}")
    print(f"\n--- FINAL ---\n{r['final']}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--model", default="gemma4-26b")
    ap.add_argument("--budget", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="raw JSON only")
    a = ap.parse_args()
    r = probe(a.task, a.model, a.budget)
    print(json.dumps(r, indent=2, default=str)) if a.json else _print(r)
