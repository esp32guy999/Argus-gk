"""Lane coverage shim — full contract lives in test_media_remonitor.py.

python tests/test_media_acquire.py
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.tools import media_acquire
    assert hasattr(media_acquire, "tools") and hasattr(media_acquire, "has_any")
    # Detailed behaviour covered by tests/test_media_remonitor.py (shared fake server).
    print("PASS: media_acquire module importable (contract in test_media_remonitor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
