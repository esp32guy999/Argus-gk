"""Doc/coverage enforcement — mechanical guardrails so docs can't silently rot.

A written "remember to update docs" mandate isn't enforceable; this is. Runs in the
suite, so green-is-the-bar enforces it for any committer (human or the Forge CC).
    python tests/test_docs.py

Asserts:
1. Every tool lane in argus/tools/ is exercised by at least one test (imported by a
   tests/test_*.py). A new lane with no test fails here.
2. The onboarding docs exist (SPEC.md, CLAUDE.md, docs/CONTRIBUTING.md).
"""
from __future__ import annotations
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    ok = True

    # 1. every lane is referenced by some test
    lanes = sorted(p.stem for p in (ROOT / "argus" / "tools").glob("*.py")
                   if p.stem != "__init__")
    test_files = list((ROOT / "tests").glob("test_*.py"))
    test_blob = "\n".join(t.read_text() for t in test_files)
    untested = [l for l in lanes if f" {l}" not in test_blob and f".{l}" not in test_blob
                and f"import {l}" not in test_blob]
    if untested:
        print(f"FAIL: tool lanes with no test referencing them: {untested}")
        print("      → add tests/test_<lane>.py (see docs/CONTRIBUTING.md)")
        ok = False
    else:
        print(f"PASS: all {len(lanes)} tool lanes are referenced by a test")

    # 2. onboarding docs present
    required = ["SPEC.md", "CLAUDE.md", "docs/CONTRIBUTING.md"]
    missing = [d for d in required if not (ROOT / d).is_file()]
    if missing:
        print(f"FAIL: missing onboarding docs: {missing}")
        ok = False
    else:
        print("PASS: onboarding docs present (SPEC, CLAUDE, CONTRIBUTING)")

    print("\nALL DOC TESTS PASSED" if ok else "\nDOC TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
