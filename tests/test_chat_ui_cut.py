"""Guardrail: the inbox client must not grow another SSE/healer farm."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui/static/app.js").read_text()
CHAT = (ROOT / "ui/static/chat.js").read_text()
INDEX = (ROOT / "ui/static/index.html").read_text()


def test_chat_js_is_the_inbox():
    assert "ArgusChat" in CHAT
    assert "/turn_status" in CHAT
    assert "client_msg_id" in CHAT
    assert "new EventSource" not in CHAT
    assert "lastDeltaTs" not in CHAT
    assert "pendingResolve" not in CHAT
    assert "sendRoundtable" not in CHAT


def test_app_js_has_no_turn_state_machine():
    assert "new EventSource" not in APP
    assert "function sendRoundtable" not in APP
    assert "function connectEvents" not in APP
    assert "function _pollSync" not in APP
    assert "lastDeltaTs" not in APP
    assert "Chat lives in chat.js" in APP


def test_index_loads_chat_before_shell():
    assert "chat.js" in INDEX
    assert INDEX.index("chat.js") < INDEX.index("app.js")
