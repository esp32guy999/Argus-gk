"""PWA focus gate for reply toasts. Standalone: python3 tests/test_pwa_presence.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from argus import pwa_presence as p

ok = 0


def check(label, got, want):
    global ok
    assert got == want, f"FAIL {label}: got {got!r}, want {want!r}"
    ok += 1
    print(f"  ✓ {label}")


def main() -> int:
    p.reset()
    print("default / stale:")
    check("no report → not focused", p.is_focused(now=100.0), False)

    print("visible heartbeat:")
    p.note(True, now=100.0)
    check("fresh visible → focused", p.is_focused(now=100.0), True)
    check("within TTL → focused", p.is_focused(now=144.0), True)
    check("past TTL → not focused", p.is_focused(now=146.0), False)

    print("minimized:")
    p.note(False, now=200.0)
    check("hidden report → not focused", p.is_focused(now=200.0), False)
    check("hidden stays not focused", p.is_focused(now=210.0), False)

    print("resume:")
    p.note(True, now=300.0)
    check("visible again → focused", p.is_focused(now=300.0), True)

    print("preview:")
    check("empty", p.preview("   \n  "), "")
    check("short", p.preview("hello"), "hello")
    check("collapse ws", p.preview("a   b\nc"), "a b c")
    long = "x" * 200
    prev = p.preview(long, limit=20)
    check("truncated len", len(prev), 20)
    check("ellipsis", prev.endswith("…"), True)

    print(f"\nALL {ok} PWA PRESENCE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
