"""Contract tests for SuperGrok OAuth path (argus/grok_code.py).

Pure helpers only — live `grok -p` needs OAuth + network.
    python tests/test_grok_code.py
"""
from __future__ import annotations
import json, os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import grok_code as gc

    # 1. env scrub: API keys must not reach the child (OAuth must win)
    os.environ["XAI_API_KEY"] = "should-not-pass"
    os.environ["GROK_CODE_XAI_API_KEY"] = "also-no"
    os.environ["KEEP_ME"] = "yes"
    env = gc._clean_env()
    assert "XAI_API_KEY" not in env, env
    assert "GROK_CODE_XAI_API_KEY" not in env, env
    assert env.get("KEEP_ME") == "yes"
    print("PASS: _clean_env strips API keys, keeps unrelated vars")

    # 2. sid persistence
    d = tempfile.mkdtemp()
    gc.SESSIONS_FILE = Path(d) / ".grok_sessions.json"
    gc._save_sid("conv-a", "sid-aaa")
    gc._save_sid("conv-b", "sid-bbb")
    assert gc._load_sids() == {"conv-a": "sid-aaa", "conv-b": "sid-bbb"}
    gc._clear_sid("conv-a")
    assert "conv-a" not in gc._load_sids()
    print("PASS: per-conversation sid persistence + clear")

    # 3. oauth_ready reads auth.json shape
    home = tempfile.mkdtemp()
    gc.AUTH_FILE = Path(home) / "auth.json"
    assert gc.oauth_ready() is False
    gc.AUTH_FILE.write_text(json.dumps({
        "https://auth.x.ai::client": {
            "auth_mode": "oidc",
            "refresh_token": "x",
        }
    }))
    assert gc.oauth_ready() is True
    print("PASS: oauth_ready detects OIDC auth.json")

    # 4. _parse_event — stream deltas + whole assistant + result
    items = gc._parse_event({
        "type": "system", "subtype": "init", "session_id": "sess-1",
        "apiKeySource": "oauth",
    })
    assert items == [("session", "sess-1")], items

    items = gc._parse_event({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "text_delta", "text": "Hi"}},
    })
    assert items == [("text_delta", "Hi")], items

    items = gc._parse_event({
        "type": "stream_event",
        "event": {"type": "content_block_start",
                  "content_block": {"type": "tool_use", "name": "run_terminal_command",
                                    "input": {}}},
    })
    assert items == [("event", {"kind": "tool_use", "name": "run_terminal_command",
                                "input": {}})], items

    items = gc._parse_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Hello"},
        ]},
    })
    assert items == [
        ("event", {"kind": "thinking", "text": "hmm"}),
        ("text", "Hello"),
    ], items

    items = gc._parse_event({
        "type": "result", "is_error": False, "duration_ms": 1200, "subtype": "success",
    })
    assert items == [("done", {"is_error": False, "duration_ms": 1200,
                               "subtype": "success"})], items
    print("PASS: _parse_event maps stream + assistant + result")

    # 5. one session object per conversation
    gc._SESSIONS.clear()
    s1 = gc._SESSIONS.setdefault("x", gc.GrokSession("x"))
    s2 = gc._SESSIONS.setdefault("x", gc.GrokSession("x"))
    assert s1 is s2
    print("PASS: one session per conversation id")

    print("\nALL CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
