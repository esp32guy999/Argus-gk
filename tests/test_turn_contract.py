"""Turn contract: classify, empty retry prompts, NO_REPLY strip (offline pure).

python tests/test_turn_contract.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.loop import (
        BLOCKED_EMPTY_MSG,
        NO_REPLY_TOKEN,
        SYSTEM_PROMPT,
        classify_turn,
        empty_retry_prompt,
        is_no_reply,
        public_turn_payload,
        resolve_turn_text,
        _apply_empty_and_silent,
    )

    # --- classify ---
    assert classify_turn("", 0) == "EMPTY"
    assert classify_turn(None, 0) == "EMPTY"
    assert classify_turn("   ", 0) == "EMPTY"
    assert classify_turn("hello", 0) == "REPLY"
    assert classify_turn(NO_REPLY_TOKEN, 0) == "NO_REPLY"
    assert classify_turn("no_reply", 0) == "NO_REPLY"
    assert classify_turn("`NO_REPLY`", 0) == "NO_REPLY"
    assert classify_turn("NO_REPLY\n", 0) == "NO_REPLY"
    assert classify_turn("please NO_REPLY later", 0) == "REPLY"  # not whole message
    assert classify_turn("", 2) == "TOOL_ONLY"
    assert classify_turn(NO_REPLY_TOKEN, 1) == "TOOL_ONLY"
    assert classify_turn("done", 3) == "REPLY"
    print("PASS: classify_turn covers EMPTY / REPLY / NO_REPLY / TOOL_ONLY")

    # --- is_no_reply ---
    assert is_no_reply("NO_REPLY")
    assert is_no_reply("  NO_REPLY.  ")
    assert not is_no_reply("")
    assert not is_no_reply("I will NO_REPLY if asked")
    print("PASS: is_no_reply is whole-message only")

    # --- resolve / public payload ---
    text, kind = resolve_turn_text("NO_REPLY", 0)
    assert text == "" and kind == "NO_REPLY"
    text, kind = resolve_turn_text("", 0)
    assert text == "" and kind == "EMPTY"
    text, kind = resolve_turn_text("hi", 0)
    assert text == "hi" and kind == "REPLY"
    text, kind = resolve_turn_text("", 2)
    assert text == "" and kind == "TOOL_ONLY"

    p = public_turn_payload("NO_REPLY", 0)
    assert p["silent"] is True and p["text"] == "" and p["kind"] == "NO_REPLY"
    p = public_turn_payload("answer", 0)
    assert p["silent"] is False and p["text"] == "answer"
    p = public_turn_payload("", 0)
    assert p["empty"] is True
    print("PASS: resolve_turn_text + public_turn_payload")

    # --- empty → blocked after retry flag ---
    vis, k = _apply_empty_and_silent("", 0, retried_empty=True)
    assert k == "BLOCKED" and vis == BLOCKED_EMPTY_MSG
    vis, k = _apply_empty_and_silent("", 0, retried_empty=False)
    assert k == "EMPTY" and vis == ""
    vis, k = _apply_empty_and_silent("NO_REPLY", 0, retried_empty=False)
    assert k == "NO_REPLY" and vis == ""
    vis, k = _apply_empty_and_silent("ok", 0, retried_empty=False)
    assert k == "REPLY" and vis == "ok"
    print("PASS: empty after retry escalates to BLOCKED; NO_REPLY strips")

    # --- empty retry prompt teaches the contract ---
    ep = empty_retry_prompt("what time is it?")
    assert "what time is it?" in ep
    assert NO_REPLY_TOKEN in ep
    assert "Empty output is not allowed" in ep
    print("PASS: empty_retry_prompt")

    # --- system prompt carries the turn contract ---
    assert "Turn contract" in SYSTEM_PROMPT
    assert "NO_REPLY" in SYSTEM_PROMPT
    assert "Empty output" in SYSTEM_PROMPT
    print("PASS: SYSTEM_PROMPT documents turn contract")

    # --- anti-stall still imported cleanly ---
    from argus.loop import _looks_unfinished
    assert not _looks_unfinished("", 0)  # empty is turn-contract, not announce-nudge
    assert _looks_unfinished("Let me check that.", 0)
    print("PASS: empty does not trigger announce unfinished")

    print("\nALL TURN CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
