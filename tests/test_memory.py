"""Contract test for curated-doc memory (argus/memory.py) + the lookup_memory tool.

Offline: a stub embed_fn (hashed bag-of-words) stands in for nomic so cosine ranks
by lexical overlap. Runnable standalone:  python tests/test_memory.py  (exit 0 = pass)
"""
from __future__ import annotations
import os, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stub_embed(texts):
    """Deterministic-within-process hashed bag-of-words vectors (256-dim)."""
    out = []
    for t in texts:
        v = [0.0] * 256
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            v[hash(w) % 256] += 1.0
        out.append(v)
    return out


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus import memory
    from argus.tools import native

    d = tempfile.mkdtemp()
    docs = {
        "hosts.md": "# Network\nHost nyx is at 100.127.86.19 and runs Home Assistant on port 8123.\n",
        "printer.md": "# Printer\nThe Bambu P1S uses PLA filament and a 0.4mm nozzle.\n",
        "media.md": "# Media\nglassgarden runs Radarr on port 7878 for movies.\n",
    }
    for name, body in docs.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(body)

    # 1. build over a directory -> chunks carry source + heading context
    idx = memory.DocMemory.build([d], embed_fn=stub_embed)
    assert len(idx.chunks) >= 3, idx.chunks
    assert any("Network" in c["text"] and "hosts.md" in c["text"] for c in idx.chunks)
    print(f"PASS: built index of {len(idx.chunks)} chunks with source+heading context")

    # 2. search ranks the lexically-relevant chunk first
    hits = idx.search("what port does home assistant on nyx use", k=3, embed_fn=stub_embed)
    assert hits and hits[0]["source"] == "hosts.md", hits
    assert "8123" in hits[0]["text"], hits[0]
    print("PASS: search ranks the relevant chunk on top")

    # 3. a different query routes to a different doc
    hits2 = idx.search("which host runs radarr for movies", k=3, embed_fn=stub_embed)
    assert hits2[0]["source"] == "media.md", hits2
    print("PASS: distinct query -> distinct source")

    # 4. result shape: source/text/score
    h = hits[0]
    assert set(h) == {"source", "text", "score"} and isinstance(h["score"], float), h
    print("PASS: result contract {source, text, score}")

    # 5. empty index -> no hits, no crash
    assert memory.DocMemory([], []).search("anything", embed_fn=stub_embed) == []
    print("PASS: empty index returns []")

    # 6. lookup_memory tool: graceful when index unavailable
    memory._INDEX = None
    try:
        native.lookup_memory("nyx ip")
        print("FAIL: expected ModelRetry when index is None"); return 1
    except ModelRetry as e:
        assert "unavailable" in str(e), str(e)
    print("PASS: lookup_memory teaches gracefully when index down")

    # 7. lookup_memory tool: returns hits when index is set
    memory._INDEX = idx
    res = native.lookup_memory("home assistant port on nyx")
    assert isinstance(res, list) and res[0]["source"] == "hosts.md", res
    memory._INDEX = None
    print("PASS: lookup_memory returns ranked hits when index is up")

    # 8. the tool is registered in the native lane with memory tags
    lm = {t.name: t for t in native.tools()}["lookup_memory"]
    assert "memory" in lm.tags and lm.func is native.lookup_memory, lm.tags
    print("PASS: lookup_memory registered in native lane")

    print("\nALL MEMORY CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
