"""Contract test for semantic select() (argus/semantic.py + registry delegation).

Deterministic + offline: injects a fake embedder over a tiny keyword axis-space, so
ranking is predictable without the embeddings server.
Runnable standalone:  python tests/test_semantic_select.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AXES = ["home", "light", "movie", "media", "time", "clock"]


def fake_embed(texts):
    """Map each string to a 0/1 vector over keyword axes -> deterministic cosine."""
    return [[1.0 if ax in s.lower() else 0.0 for ax in AXES] for s in texts]


def main() -> int:
    from argus.registry import Registry, Tool
    from argus.semantic import SemanticSelector

    def f(**k):
        return "ok"

    tools = [
        Tool("turn_on_light", "Turn on a light in the home", ["home"], f),
        Tool("list_movies", "List movies in the media library", ["media"], f),
        Tool("get_time", "Get the current time on the clock", ["time"], f),
    ]

    # 1. ranking picks the relevant tool
    sel = SemanticSelector(tools, embed_fn=fake_embed, top_k=1)
    assert sel.select("please turn on the light")[0].name == "turn_on_light"
    assert sel.select("what movies do I have")[0].name == "list_movies"
    assert sel.select("what time is it")[0].name == "get_time"
    print("PASS: semantic ranking picks the relevant tool")

    # 2. registry.select delegates to the semantic selector
    r = Registry(); r.add_provider(tools)
    r.semantic = SemanticSelector(tools, embed_fn=fake_embed, top_k=1)
    sub = r.select("turn on the light")
    assert len(sub) == 1 and sub[0].name == "turn_on_light", [t.name for t in sub]
    print("PASS: registry.select() delegates to semantic")

    # 3. graceful fallback: a broken embedder -> registry returns ALL tools
    class Boom:
        def select(self, context):
            raise RuntimeError("embeddings down")
    r.semantic = Boom()
    assert len(r.select("anything")) == len(r.all())
    print("PASS: embeddings failure -> falls back to all tools")

    print("\nALL SEMANTIC SELECT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
