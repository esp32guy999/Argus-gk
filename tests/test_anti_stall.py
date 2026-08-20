"""Contract test for the anti-stall helpers in argus/loop.py (offline, pure)."""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.loop import _looks_unfinished, _nudge_prompt, _budget_summary, _tool_retry_summary

    # announce-then-stop: zero calls + trailing announcement -> nudge
    for text in ("Let me check that for you.",
                 "Sure — I'll look that up now",
                 "One moment, checking the weather…",
                 "Working on it!"):
        assert _looks_unfinished(text, 0), text
    print("PASS: trailing announcements with zero calls are flagged")

    # safe cases: tool calls happened, or the phrase isn't the ENDING
    assert not _looks_unfinished("Let me check that for you.", 2), "calls made -> no nudge"
    assert not _looks_unfinished(
        "I'll always remember the day the anvil sang.\nIts iron heart rang true.", 0
    ), "poem containing I'll must not trigger"
    assert not _looks_unfinished("The workshop light is now on.", 0)
    assert not _looks_unfinished("", 0)
    print("PASS: completed answers, poems, and tool-calling runs are left alone")

    # nudge prompt carries the original ask + the offending tail
    p = _nudge_prompt("turn on the light", "Let me do that now.")
    assert "turn on the light" in p and "Let me do that now." in p and "CALL" in p
    print("PASS: nudge prompt includes task + quoted tail")

    # budget summary names the progress, deduped in order
    s = _budget_summary(8, ["weather_current", "HassListAddItem", "weather_current"])
    assert "8-turn" in s and "weather_current → HassListAddItem" in s
    s = _budget_summary(8, [])
    assert "no tool calls" in s
    print("PASS: budget summary reports deduped progress")

    r = _tool_retry_summary(
        RuntimeError("Tool 'list_dir' exceeded max retries"),
        ["list_dir", "read_file", "list_dir"],
    )
    assert "list_dir" in r and "Stopped" in r and "read_file" in r
    assert "exception" not in r.lower()
    print("PASS: tool retry exhaustion is a sentence, not a crash")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
