"""Contract test for the notes lane (argus/tools/notes.py). Offline.
    python tests/test_notes.py     (exit 0 = pass)
"""
from __future__ import annotations
import os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import notes
    from argus import memory

    d = tempfile.mkdtemp()
    notes.NOTES_DIR = pathlib.Path(d)

    def stub_embed(texts):
        return [[float(len(t)), 1.0] for t in texts]

    memory._INDEX = memory.DocMemory([], [], stub_embed)  # live index to append into
    try:
        # 1. save_note writes a markdown file containing the note
        out = notes.save_note("PETG on the P1S: nozzle 240C, bed 70C.")
        assert out["saved"] and out["file"].endswith(".md"), out
        files = list(pathlib.Path(d).glob("*.md"))
        assert len(files) == 1 and "240C" in files[0].read_text(), files
        print("PASS: save_note writes a note file")

        # 2. it's appended to the LIVE index (searchable without restart)
        assert len(memory._INDEX.chunks) >= 1, "note not added to live index"
        hits = memory._INDEX.search("nozzle temperature", k=1, embed_fn=stub_embed)
        assert hits and "240C" in hits[0]["text"], hits
        print("PASS: note immediately searchable via the live index")

        # 3. empty note -> teaching ModelRetry
        try:
            notes.save_note("   ")
            print("FAIL: expected ModelRetry on empty note"); return 1
        except ModelRetry:
            pass
        print("PASS: empty note -> ModelRetry")

        # 4. registered with notes tags
        t = {x.name: x for x in notes.tools()}["save_note"]
        assert "notes" in t.tags and t.func is notes.save_note, t.tags
        print("PASS: save_note registered")

        print("\nALL NOTES CONTRACT TESTS PASSED")
        return 0
    finally:
        memory._INDEX = None


if __name__ == "__main__":
    sys.exit(main())
