"""Contract tests for the inbox chat helpers (specs/chat-client.md).

Standalone:  python tests/test_chat_client.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.chat_client import (
        is_inviteable, normalize_to, normalize_client_msg_id, assistant_persist_text,
        format_turn_line, push_turn_tail,
    )

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

    assert assistant_persist_text("Hello") == "Hello"
    assert assistant_persist_text("_(running `edit_file`…)_", n_tools=3).startswith("_(Finished 3")
    assert "Error" in assistant_persist_text("partial", error="boom")
    assert assistant_persist_text("", error="boom") == "[Error: boom]"
    assert assistant_persist_text("", cancelled=True) == "[Cancelled]"
    assert assistant_persist_text("", n_tools=2).startswith("_(Finished 2")
    assert assistant_persist_text("") == "[No reply]"
    print("PASS: assistant_persist_text never returns empty")

    assert format_turn_line("tool", "edit_file") == "using edit_file"
    assert format_turn_line("writing", "x") == "writing"
    assert format_turn_line("working", "still working (45s)") == "still working (45s)"
    t = []
    t = push_turn_tail(t, "starting", "")
    t = push_turn_tail(t, "tool", "edit_file")
    t = push_turn_tail(t, "tool", "edit_file")  # consecutive dup
    t = push_turn_tail(t, "tool", "run_command")
    t = push_turn_tail(t, "working", "still working (3m)")
    t = push_turn_tail(t, "writing", "")
    assert t == [
        "using edit_file",
        "using run_command",
        "still working (3m)",
        "writing",
    ]
    print("PASS: turn tail keeps last distinct activity lines")

    print("\nALL CHAT CLIENT HELPER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
