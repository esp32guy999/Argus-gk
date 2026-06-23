"""Contract test for persistent Claude Code sessions (argus/claude_code.py).

Covers the pure helpers (env isolation, sid persistence, transcript check) — the
subprocess itself needs a real `claude` binary so it's exercised live, not here.
    python tests/test_claude_code.py
"""
from __future__ import annotations
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import claude_code as cc

    # 1. env isolation: parent CLAUDE_CODE_* / CLAUDECODE dropped, and any inherited
    #    CLAUDE_CONFIG_DIR is popped so the subprocess uses the default ~/.claude
    #    (one shared credential store — see docs/postmortem-oauth-token-revocation.md).
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CONFIG_DIR"] = "/some/isolated/dir"
    os.environ["KEEP_ME"] = "yes"
    env = cc._clean_env()
    assert not any(k.startswith("CLAUDE_CODE") for k in env), "must drop CLAUDE_CODE_*"
    assert "CLAUDECODE" not in env, "must drop CLAUDECODE"
    assert env.get("KEEP_ME") == "yes", "unrelated vars preserved"
    assert "CLAUDE_CONFIG_DIR" not in env, "must NOT set CLAUDE_CONFIG_DIR (use default ~/.claude)"
    print("PASS: _clean_env drops nested-cc vars and inherited CLAUDE_CONFIG_DIR")

    # 2. sid persistence roundtrip (per conversation)
    d = tempfile.mkdtemp()
    cc.SESSIONS_FILE = Path(d) / ".cc_sessions.json"
    cc._save_sid("conv-a", "sid-aaa")
    cc._save_sid("conv-b", "sid-bbb")
    assert cc._load_sids() == {"conv-a": "sid-aaa", "conv-b": "sid-bbb"}, cc._load_sids()
    cc._save_sid("conv-a", "sid-aaa2")   # overwrite same conv
    assert cc._load_sids()["conv-a"] == "sid-aaa2", "sid overwrites per conversation"
    print("PASS: per-conversation sid persistence")

    # 3. transcript existence gate — resume only when the real transcript file
    #    exists under ~/.claude (the canonical config dir; see the OAuth post-mortem).
    home = tempfile.mkdtemp()
    os.environ["HOME"] = home
    cc._WORKDIR_SLUG = "-tmp-work"
    assert cc._transcript_exists("nope") is False
    tdir = Path(home) / ".claude" / "projects" / "-tmp-work"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "real-sid.jsonl").write_text("{}")
    assert cc._transcript_exists("real-sid") is True
    print("PASS: _transcript_exists gates resume correctly")

    # 4. session manager returns the SAME session per conversation id
    cc._SESSIONS.clear()
    s1 = cc._SESSIONS.setdefault("x", cc.CCSession("x"))
    s2 = cc._SESSIONS.setdefault("x", cc.CCSession("x"))
    assert s1 is s2, "one persistent session object per conversation"
    print("PASS: one session per conversation id")

    # 5. _parse_event maps stream-json events to structured activity markers
    # assistant: text + thinking + tool_use in one event, in order
    asst = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hello"},
        {"type": "thinking", "thinking": "let me think"},
        {"type": "tool_use", "name": "Read", "input": {"path": "/tmp/x"}},
        {"type": "text", "text": "   "},          # whitespace-only text is dropped
    ]}}
    items = cc._parse_event(asst)
    assert items == [
        ("text", "hello"),
        ("event", {"kind": "thinking", "text": "let me think"}),
        ("event", {"kind": "tool_use", "name": "Read", "input": {"path": "/tmp/x"}}),
    ], items
    print("PASS: _parse_event yields text + thinking + tool_use (drops blank text)")

    # tool_result rides on a user-role event; string content passes through, truncated to 2000
    long = "z" * 5000
    user_evt = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": long},
        {"type": "tool_result", "content": [{"type": "text", "text": "obj"}]},  # non-str -> json
    ]}}
    items = cc._parse_event(user_evt)
    assert items[0][0] == "event" and items[0][1]["kind"] == "tool_result"
    assert len(items[0][1]["text"]) == 2000, "tool_result truncated to 2000 chars"
    assert isinstance(items[1][1]["text"], str) and "obj" in items[1][1]["text"], "non-str content json-encoded"
    print("PASS: _parse_event truncates tool_result and json-encodes non-str content")

    # result event carries duration on a done marker
    res = {"type": "result", "is_error": False, "duration_ms": 12345}
    items = cc._parse_event(res)
    assert items == [("done", {"is_error": False, "duration_ms": 12345})], items
    # missing duration degrades to None (the frontend tolerates it)
    assert cc._parse_event({"type": "result"}) == [
        ("done", {"is_error": False, "duration_ms": None})], "graceful when fields absent"
    print("PASS: _parse_event emits done with duration_ms")

    # 6. _image_block: UI attachment -> Anthropic image content block (vision)
    png = {"isImage": True, "dataUrl": "data:image/png;base64,iVBORw0KGgo="}
    assert cc._image_block(png) == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }, cc._image_block(png)
    assert cc._image_block({"isImage": True, "dataUrl": "data:image/jpeg;base64,/9j/"})[
        "source"]["media_type"] == "image/jpeg"
    assert cc._image_block({"isImage": False, "dataUrl": "data:application/pdf;base64,JV"}) is None, "non-image skipped"
    assert cc._image_block({"isImage": True, "dataUrl": "data:image/svg+xml;base64,PHN2"}) is None, "unsupported type skipped"
    assert cc._image_block({"isImage": True, "dataUrl": "https://x/y.png"}) is None, "non-data url skipped"
    assert cc._image_block(None) is None and cc._image_block({}) is None
    print("PASS: _image_block parses base64 images and rejects non-images/bad urls")

    print("\nALL CLAUDE_CODE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
