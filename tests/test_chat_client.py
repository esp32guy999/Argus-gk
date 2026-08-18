"""Contract tests for the inbox chat helpers (specs/chat-client.md).

Standalone:  python tests/test_chat_client.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.chat_client import is_inviteable, normalize_to, normalize_client_msg_id

    assert is_inviteable("grok")
    assert is_inviteable("gemma4-26b")
    assert is_inviteable("claude")
    assert not is_inviteable("claude-code")
    assert not is_inviteable("z-image-edit")
    assert not is_inviteable("z-video")
    assert not is_inviteable("")
    assert not is_inviteable(None)
    print("PASS: inviteable filter")

    assert normalize_to({}, "qwen3-next-80b") == ["qwen3-next-80b"]
    assert normalize_to({"model": "grok"}, "x") == ["grok"]
    # old client can still address legacy claude-code via `model`
    assert normalize_to({"model": "claude-code"}, "x") == ["claude-code"]
    assert normalize_to({"to": ["grok", "gemma4-26b", "grok"]}, "x") == [
        "grok", "gemma4-26b"]
    assert normalize_to({"to": "claude"}, "x") == ["claude"]
    assert normalize_to({"to": ["z-klein", "claude-code", "grok"]}, "x") == ["grok"]
    assert normalize_to({"to": ["z-image-edit"]}, "x") == []
    print("PASS: normalize_to")

    assert normalize_client_msg_id(None) is None
    assert normalize_client_msg_id("") is None
    assert normalize_client_msg_id("  ") is None
    assert normalize_client_msg_id("abc") == "abc"
    print("PASS: client_msg_id normalize")

    print("\nALL CHAT CLIENT HELPER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
