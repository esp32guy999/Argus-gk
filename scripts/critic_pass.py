#!/usr/bin/env python3
"""Critic-only MVP for the Reflections loop (docs/DESIGN-reflections-loop.md).

Read-only. Builds a provenance digest from this week's real exhaust (jobs table, loop
audit log, ISSUES.md, recent user messages), runs the Critic (structured output), then
MECHANICALLY drops any diagnosis whose evidence citations don't exist verbatim in the
digest. Answers the one engineering question: does evidence-gating keep the Critic
silent, or does it hallucinate phantom patterns out of loose tokens?

Run:  .venv/bin/python scripts/critic_pass.py
Env:  CRITIC_MODEL (default qwen3-coder-30b), CRITIC_BASE (default :9090).
"""
from __future__ import annotations
import json, os, sqlite3, sys, time
from typing import List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pydantic import BaseModel, Field                                  # noqa: E402
from pydantic_ai import Agent                                          # noqa: E402
from argus.loop import make_model                                      # noqa: E402

DB = os.path.join(ROOT, "argus.db")
AUDIT = os.path.expanduser("~/.claude/argus-loop-audit.jsonl")
ISSUES = os.path.join(ROOT, "docs/ISSUES.md")
# The DESIGNATED AUDITOR is qwen3-next-80b. The 30b coder is benched from evaluation:
# it under-fires (returned 0 diagnoses where the 80b found 3 grounded ones on the same
# digest). 30b stays the coding DOER (code_edit lane); it is not used to JUDGE.
MODEL = os.environ.get("CRITIC_MODEL", "qwen3-next-80b")
BASE = os.environ.get("CRITIC_BASE", "http://localhost:9090/v1")


# --- the Critic's structured finding (Shane's schema) ----------------------------
class Diagnosis(BaseModel):
    title: str
    evidence_citations: List[str] = Field(
        description="Keys that appear VERBATIM as a [bracketed key] in the digest, "
                    "e.g. job:dl-leftover-salmon, audit:2, msg:857. Never invent one.")
    confidence_score: float = Field(ge=0.0, le=1.0)
    diagnostic_reasoning: str = Field(description="Why this is a SYSTEMATIC pattern, not a one-off anomaly.")


class Diagnoses(BaseModel):
    items: List[Diagnosis]


# --- provenance digest: every evidence unit prefixed by its citation key ---------
def build_digest() -> tuple[str, set[str]]:
    keys: set[str] = set()
    out: List[str] = []
    now = time.time()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

    out.append("== JOBS (work ledger) ==")
    for r in c.execute("SELECT * FROM jobs ORDER BY updated_ts DESC"):
        k = f"job:{r['id']}"; keys.add(k)
        age = (now - (r["created_ts"] or now)) / 86400
        out.append(f"[{k}] {r['kind']}/{r['category']} state={r['state']} "
                   f"progress={r['progress']} age={age:.1f}d detail={(r['detail'] or '')[:80]!r}")

    out.append("\n== LOOP AUDIT (stop-hook decisions) ==")
    if os.path.exists(AUDIT):
        for i, ln in enumerate(open(AUDIT)):
            try: a = json.loads(ln)
            except ValueError: continue
            k = f"audit:{i}"; keys.add(k)
            out.append(f"[{k}] cont={a.get('cont')} reason={a.get('reason')!r} "
                       f"job={a.get('job_id')} consecutive={a.get('consecutive')}")

    out.append("\n== ISSUES.md (deferred backlog) ==")
    if os.path.exists(ISSUES):
        for i, ln in enumerate(open(ISSUES)):
            s = ln.rstrip()
            if s.lstrip().startswith(("-", "*")) and len(s.strip()) > 4:
                k = f"issue:{i}"; keys.add(k); out.append(f"[{k}] {s.strip()[:120]}")

    out.append("\n== RECENT USER MESSAGES (automation signal) ==")
    for r in c.execute("SELECT id, content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 30"):
        k = f"msg:{r['id']}"; keys.add(k)
        out.append(f"[{k}] {(r['content'] or '')[:110]!r}")

    return "\n".join(out), keys


CRITIC_SYS = (
    "You are the CRITIC stage of a reflection loop. Your ONLY job is to diagnose "
    "SYSTEMATIC, RECURRING patterns in the provenance digest — things that happened "
    "repeatedly, or are persistently stuck/failing. You do NOT propose fixes or actions.\n"
    "HARD RULES:\n"
    "1. Every diagnosis MUST include >=1 evidence_citation that appears VERBATIM as a "
    "[bracketed key] in the digest (job:..., audit:N, issue:N, msg:N). Inventing a citation "
    "is the worst failure.\n"
    "2. A one-off is NOT a pattern. Only flag something that recurs or is systemic across "
    "multiple evidence units.\n"
    "3. NO generic advice ('organize your code', 'add tests', 'improve docs'). Only patterns "
    "grounded in THIS specific data.\n"
    "4. Most runs you should return FEW or ZERO diagnoses. An empty list is the correct, "
    "expected answer when there is no genuine recurring pattern. Do not pad.\n"
    "5. Directional, not a verdict: confidence_score reflects how strongly the cited evidence "
    "supports the pattern."
)


def main():
    digest, keys = build_digest()
    with open("/tmp/critic_digest.txt", "w") as f: f.write(digest)
    print(f"digest: {len(keys)} citable keys, {len(digest)} chars (saved /tmp/critic_digest.txt)\n")

    agent = Agent(make_model(MODEL, BASE), output_type=Diagnoses, system_prompt=CRITIC_SYS)
    t0 = time.time()
    try:
        res = agent.run_sync("Provenance digest follows. Diagnose only systematic patterns.\n\n" + digest)
        items = res.output.items
    except Exception as e:
        print(f"CRITIC ERROR ({MODEL}): {type(e).__name__}: {str(e)[:300]}"); return
    dt = time.time() - t0

    # --- THE MECHANICAL FLOOR: a citation must exist verbatim, or the diagnosis is dropped ---
    kept, dropped = [], []
    for d in items:
        valid = [c for c in d.evidence_citations if c in keys]
        (kept if valid else dropped).append((d, valid))

    print(f"Critic [{MODEL}] returned {len(items)} diagnoses in {dt:.1f}s — "
          f"{len(kept)} survived evidence-gating, {len(dropped)} DROPPED (phantom citations).\n")

    md = [f"# Critic pass — {time.strftime('%Y-%m-%d %H:%M')}  (model: {MODEL})\n"]
    for d, valid in kept:
        md.append(f"## {d.title}  · conf {d.confidence_score:.2f}")
        md.append(f"- **evidence:** {', '.join(valid)}")
        md.append(f"- **why systematic:** {d.diagnostic_reasoning}\n")
    if not kept:
        md.append("_(no diagnoses survived the floor — silence)_\n")
    if dropped:
        md.append("---\n### DROPPED by the floor (invalid/phantom citations):")
        for d, _ in dropped:
            md.append(f"- {d.title}  — cited {d.evidence_citations}")
    report = "\n".join(md)
    with open("/tmp/critic_pass_out.md", "w") as f: f.write(report)
    print(report)


if __name__ == "__main__":
    main()
