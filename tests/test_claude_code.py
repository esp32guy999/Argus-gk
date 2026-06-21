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

    # 1. env isolation: parent CLAUDE_CODE_* / CLAUDECODE dropped, config dir set
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
    os.environ["CLAUDECODE"] = "1"
    os.environ["KEEP_ME"] = "yes"
    env = cc._clean_env()
    assert not any(k.startswith("CLAUDE_CODE") for k in env), "must drop CLAUDE_CODE_*"
    assert "CLAUDECODE" not in env, "must drop CLAUDECODE"
    assert env.get("KEEP_ME") == "yes", "unrelated vars preserved"
    assert env.get("CLAUDE_CONFIG_DIR") == cc.CONFIG_DIR, "points at isolated config dir"
    print("PASS: _clean_env drops nested-cc vars, sets isolated config dir")

    # 2. sid persistence roundtrip (per conversation)
    d = tempfile.mkdtemp()
    cc.SESSIONS_FILE = Path(d) / ".cc_sessions.json"
    cc._save_sid("conv-a", "sid-aaa")
    cc._save_sid("conv-b", "sid-bbb")
    assert cc._load_sids() == {"conv-a": "sid-aaa", "conv-b": "sid-bbb"}, cc._load_sids()
    cc._save_sid("conv-a", "sid-aaa2")   # overwrite same conv
    assert cc._load_sids()["conv-a"] == "sid-aaa2", "sid overwrites per conversation"
    print("PASS: per-conversation sid persistence")

    # 3. transcript existence gate (resume only when the transcript file is real)
    cc.CONFIG_DIR = d
    cc._WORKDIR_SLUG = "-tmp-work"
    assert cc._transcript_exists("nope") is False
    tdir = Path(d) / "projects" / "-tmp-work"
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

    print("\nALL CLAUDE_CODE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
