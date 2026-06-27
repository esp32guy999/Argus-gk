#!/usr/bin/env python3
"""Diff/code-flavored Critic — evidence-gated code review (docs/DESIGN-reflections-loop.md).

Same Critic + evidence-gating architecture as scripts/critic_pass.py, but the provenance
digest is line-numbered SOURCE, and citations are `file:line`. The mechanical floor drops
any finding whose cited line doesn't exist — which kills the classic LLM-code-review
failure mode (confidently hallucinated bugs at made-up locations).

LOOSENED (bootstrap mode): report any genuine defect, not just systematic patterns —
high recall now to clear the unaudited backlog. Grounding stays strict: no real line, no
finding. Tighten later.

Run:  .venv/bin/python scripts/code_critic.py [file1 file2 ...]
Env:  CRITIC_MODEL (default qwen3-coder-30b — code specialist, 16k ctx), CRITIC_BASE (:9090).
"""
from __future__ import annotations
import os, sys, time
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pydantic import BaseModel, Field                                  # noqa: E402
from pydantic_ai import Agent                                          # noqa: E402
from argus.loop import make_model                                      # noqa: E402

# Designated auditor: qwen3-next-80b (the 30b under-fires and is benched from evaluation;
# it stays the coding DOER, not the JUDGE). 80b is slower but it's a batch job.
MODEL = os.environ.get("CRITIC_MODEL", "qwen3-next-80b")
BASE = os.environ.get("CRITIC_BASE", "http://localhost:9090/v1")

# This week's highest-risk NEW code (safety/security/blast-radius first).
DEFAULT_TARGETS = [
    "argus/loop_guard.py",            # the loop guardrails — wrong = runaway
    "hooks/stop_ledger_heartbeat.py", # the hook that governs the live session
    "argus/tools/code_edit.py",       # the lane that writes files
    "argus/ledger.py",                # the reconciler + probes
    "argus/storage.py",               # the jobs schema/migration
]


class Finding(BaseModel):
    title: str
    file_citations: List[str] = Field(
        description="file:line keys that appear VERBATIM in the digest, e.g. "
                    "argus/loop_guard.py:88. Cite the exact line(s) the defect lives on.")
    severity: str = Field(description="low | medium | high")
    confidence_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="The concrete defect: what's wrong and what it breaks.")


class Findings(BaseModel):
    items: List[Finding]


def build_digest(targets: List[str]) -> tuple[str, set[str]]:
    keys: set[str] = set()
    out: List[str] = []
    for rel in targets:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        out.append(f"\n===== FILE: {rel} =====")
        for n, line in enumerate(open(path, encoding="utf-8", errors="replace"), start=1):
            keys.add(f"{rel}:{n}")
            out.append(f"{rel}:{n}: {line.rstrip()}")
    return "\n".join(out), keys


CRITIC_SYS = (
    "You are an evidence-gated CODE CRITIC reviewing freshly-written, unaudited code. "
    "Report GENUINE defects only: bugs, logic errors, race conditions, resource leaks, "
    "missing error handling, security holes, broken edge cases. This is a LOOSENED pass — "
    "report any real issue you find (one-offs are fine, it doesn't need to be a pattern).\n"
    "HARD RULES:\n"
    "1. Every finding MUST include >=1 file_citation that appears VERBATIM in the digest as "
    "'path:line' (e.g. argus/loop_guard.py:88). Cite the exact offending line. Inventing a "
    "citation or a line number is the worst possible failure.\n"
    "2. NO style nits, NO 'add tests', NO 'add docstrings', NO speculative 'could be cleaner'. "
    "Only concrete defects that would actually misbehave.\n"
    "3. If a file is clean, say nothing about it. An empty list is a fine answer.\n"
    "4. severity = low|medium|high by real-world impact; confidence = how sure the cited code "
    "actually has the defect."
)


def main():
    targets = sys.argv[1:] or DEFAULT_TARGETS
    digest, keys = build_digest(targets)
    with open("/tmp/code_critic_digest.txt", "w") as f: f.write(digest)
    print(f"reviewing {len(targets)} files · {len(keys)} citable lines · {len(digest)} chars "
          f"· model {MODEL}\n")

    agent = Agent(make_model(MODEL, BASE), output_type=Findings, system_prompt=CRITIC_SYS)
    t0 = time.time()
    try:
        res = agent.run_sync("Review this code. Cite file:line for every finding.\n\n" + digest)
        items = res.output.items
    except Exception as e:
        print(f"CRITIC ERROR ({MODEL}): {type(e).__name__}: {str(e)[:300]}"); return
    dt = time.time() - t0

    # THE FLOOR: a finding must cite a real, existing line — else it's a hallucination, dropped.
    kept, dropped = [], []
    for d in items:
        valid = [c for c in d.file_citations if c in keys]
        (kept if valid else dropped).append((d, valid))

    print(f"Critic [{MODEL}] returned {len(items)} findings in {dt:.1f}s — "
          f"{len(kept)} grounded, {len(dropped)} DROPPED (phantom file:line).\n")

    md = [f"# Code Critic — {time.strftime('%Y-%m-%d %H:%M')} (model {MODEL}, LOOSENED)\n"]
    for d, valid in sorted(kept, key=lambda x: -x[0].confidence_score):
        md.append(f"## [{d.severity}] {d.title}  · conf {d.confidence_score:.2f}")
        md.append(f"- **at:** {', '.join(valid)}")
        md.append(f"- **defect:** {d.reasoning}\n")
    if not kept:
        md.append("_(no grounded findings — the new code is clean, or the model under-fired)_\n")
    if dropped:
        md.append("---\n### DROPPED by the floor (phantom file:line — would-be hallucinated bugs):")
        for d, _ in dropped:
            md.append(f"- {d.title} — cited {d.file_citations}")
    report = "\n".join(md)
    with open("/tmp/code_critic_out.md", "w") as f: f.write(report)
    print(report)


if __name__ == "__main__":
    main()
