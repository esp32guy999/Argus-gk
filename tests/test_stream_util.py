"""Stream merge — no exact-echo from cumulative re-sends.

python tests/test_stream_util.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.stream_util import merge_stream_text

    # cumulative snapshots (CC-style growing full text)
    acc = merge_stream_text("", "Hello")
    acc = merge_stream_text(acc, "Hello world")
    assert acc == "Hello world", acc
    print("PASS: cumulative snapshot extends")

    acc = merge_stream_text("Hello world", "Hello world")
    assert acc == "Hello world", acc
    print("PASS: exact re-delivery does not echo")

    # classic CC bug: two full identical deliveries via naive +=
    naive = ""
    full = "## Latest Qwen\n\nline2"
    naive = naive + ("\n\n" if naive else "") + full
    naive = naive + ("\n\n" if naive else "") + full
    assert naive.count("## Latest Qwen") == 2
    fixed = merge_stream_text("", full)
    fixed = merge_stream_text(fixed, full)
    assert fixed.count("## Latest Qwen") == 1 and fixed == full, fixed
    print("PASS: double full re-send collapses to one")

    # growing cumulative snapshots
    acc = ""
    for snap in ("A", "A B", "A B C"):
        acc = merge_stream_text(acc, snap)
    assert acc == "A B C", acc
    print("PASS: growing snapshots")

    # true second paragraph
    acc = merge_stream_text("First para.", "Second para.")
    assert acc == "First para.\n\nSecond para.", acc
    print("PASS: distinct second block still appends")

    # stale shorter
    acc = merge_stream_text("Hello world", "Hello")
    assert acc == "Hello world", acc
    print("PASS: shorter stale snapshot ignored")

    print("\nALL STREAM_UTIL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
