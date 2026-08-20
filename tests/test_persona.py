"""Personas are voices independent of the serving model.

python tests/test_persona.py
"""
from __future__ import annotations
import os, sys, tempfile, shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import persona, loop

    ids = {p["id"] for p in persona.list_personas()}
    assert "argus" in ids, ids
    assert "loki" in ids, ids
    print("PASS: argus + loki listed")

    assert persona.normalize(None) == "argus"
    assert persona.normalize("") == "argus"
    assert persona.normalize("loki") == "loki"
    assert persona.normalize("../etc/passwd") == "argus"
    assert persona.normalize("no-such") == "argus"
    print("PASS: normalize unknown/unsafe → argus")

    argus = persona.voice("argus")
    loki = persona.voice("loki")
    assert "Be a person, not a brochure" in argus
    wrapped = persona.wrap_user_text("loki", "hello")
    assert wrapped.startswith("[Voice for this turn") and "hello" in wrapped
    assert persona.wrap_user_text("argus", "hello") == "hello"
    print("PASS: wrap_user_text only prefixes non-default")

    assert "Your name is **Loki**" in loki
    assert "Your name is **Loki**" not in argus
    print("PASS: voice files differ")

    p_argus = loop._system_prompt("qwen3.8-27b", persona="argus")
    p_loki = loop._system_prompt("qwen3.8-27b", persona="loki")
    assert "# Your voice" in p_argus and "# Your voice" in p_loki
    assert "Your name is **Loki**" in p_loki
    assert "Your name is **Loki**" not in p_argus
    print("PASS: Loki voice on qwen3.8 (not a Loki model)")

    # Model overlay still attaches by model id, independent of persona.
    p_orn = loop._system_prompt("ornith-35b-uncensored", persona="argus")
    assert "# This model" in p_orn
    assert "No system changes" in p_orn
    assert "Your name is **Loki**" not in p_orn
    print("PASS: ornith overlay is capability-only; Argus persona does not inherit Loki name")

    # Isolated extra persona file
    tmp = tempfile.mkdtemp()
    try:
        old = persona._DIR
        persona._DIR = tmp
        persona._cache.clear()
        open(os.path.join(tmp, "reviewer.md"), "w").write("# Reviewer\nBe terse and picky.\n")
        assert any(p["id"] == "reviewer" for p in persona.list_personas())
        assert "Be terse and picky" in persona.voice("reviewer")
        print("PASS: drop-in personas/*.md is picked up")
        persona._DIR = old
        persona._cache.clear()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        persona._DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "personas")
        persona._cache.clear()

    print("\nALL PERSONA CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
