"""Contract test for per-model soul overlays (argus/loop.py).

Defines what the overlay seam MUST do: append soul.d/<key>.md when <key> is a
substring of the model id, skip non-matching models, hot-reload on edit, and gate
code_edit tools by the same substring rule. Offline, uses a temp soul.d.
Runnable standalone:  python tests/test_soul_overlay.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import loop

    with tempfile.TemporaryDirectory() as d:
        old_soul_d = loop._SOUL_D
        loop._SOUL_D = d
        loop._overlay_cache.clear()
        try:
            # 1. no overlay files -> base prompt unchanged
            base = loop._system_prompt("ornith-35b-uncensored")
            assert "# This model" not in base
            print("PASS: empty soul.d leaves prompt unchanged")

            # 2. matching overlay appended; substring covers variant ids
            with open(os.path.join(d, "ornith-35b.md"), "w") as f:
                f.write("Be blunt.")
            for mid in ("ornith-35b-uncensored", "ornith-35b-ngram", "ornith-35b-mtp"):
                p = loop._system_prompt(mid)
                assert "# This model" in p and "Be blunt." in p, mid
            print("PASS: overlay appended for all matching model ids")

            # 3. non-matching model gets no overlay
            p = loop._system_prompt("gemma4-26b")
            assert "Be blunt." not in p
            print("PASS: non-matching model unaffected")

            # 4. hot-reload: edit takes effect without restart
            time.sleep(0.01)
            with open(os.path.join(d, "ornith-35b.md"), "w") as f:
                f.write("Be very blunt.")
            os.utime(os.path.join(d, "ornith-35b.md"))
            p = loop._system_prompt("ornith-35b-uncensored")
            assert "Be very blunt." in p and "Be blunt." not in p.replace("Be very blunt.", "")
            print("PASS: overlay hot-reloads on edit")
        finally:
            loop._SOUL_D = old_soul_d
            loop._overlay_cache.clear()

    # 5. lane gate: ornith cleared for code_edit, non-coders still excluded
    class T:
        def __init__(self, provider): self.provider = provider
    tools = [T("code_edit"), T("web")]
    kept = loop._gate_tools(tools, "ornith-35b-ngram")
    assert [t.provider for t in kept] == ["code_edit", "web"]
    kept = loop._gate_tools(tools, "gemma4-26b")
    assert [t.provider for t in kept] == ["web"]
    print("PASS: code_edit gate admits ornith, excludes non-coders")

    print("\nALL SOUL OVERLAY TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
