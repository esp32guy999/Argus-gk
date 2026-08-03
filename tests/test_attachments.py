"""Contract tests for the paperclip attachment pipeline (argus/attachments.py).

Standalone: python tests/test_attachments.py
"""
from __future__ import annotations
import base64
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _png_data_url() -> str:
    # 1x1 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def main() -> int:
    from argus import attachments as att

    tmp = Path(tempfile.mkdtemp())
    att.UPLOAD_ROOT = tmp / "uploads"

    # 1. materialize image
    mats = att.materialize([
        {"filename": "dot.png", "isImage": True, "dataUrl": _png_data_url()},
    ], conversation_id="test-conv")
    assert len(mats) == 1, mats
    assert mats[0].is_image and mats[0].path.is_file()
    assert mats[0].size > 0 and mats[0].data_b64
    print("PASS: materialize image to disk + keep base64")

    # 2. materialize text file
    body = "hello paperclip\nline2"
    text_url = "data:text/plain;base64," + base64.b64encode(body.encode()).decode()
    mats2 = att.materialize([
        {"filename": "note.txt", "isImage": False, "dataUrl": text_url},
    ], conversation_id="test-conv")
    assert mats2[0].path.read_text() == body
    print("PASS: materialize text file")

    # 3. vision content blocks — Anthropic (CC) vs ACP (Grok)
    blocks = att.vision_content_blocks("what is this?", mats, style="anthropic")
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[-1] == {"type": "text", "text": "what is this?"}
    print("PASS: vision_content_blocks anthropic image + text")

    blocks_acp = att.vision_content_blocks("what is this?", mats, style="acp")
    assert blocks_acp[0]["type"] == "image"
    assert "data" in blocks_acp[0] and "mimeType" in blocks_acp[0]
    assert "source" not in blocks_acp[0]
    print("PASS: vision_content_blocks ACP image shape for Grok")

    pf = att.write_acp_prompt_file("hi", mats)
    assert pf.is_file() and pf.stat().st_size > 10
    import json as _json
    loaded = _json.loads(pf.read_text())
    assert loaded[0]["type"] == "image" and loaded[0]["mimeType"]
    pf.unlink(missing_ok=True)
    print("PASS: write_acp_prompt_file")

    # 4. enrich_prompt includes path + text content
    enriched = att.enrich_prompt("summarize", mats2)
    assert "note.txt" in enriched and "hello paperclip" in enriched
    assert "UNTRUSTED DOCUMENT" in enriched
    print("PASS: enrich_prompt inlines text + provenance")

    # 5. size limit
    att.MAX_FILE_BYTES = 10
    try:
        att.materialize([
            {"filename": "big.bin", "isImage": False,
             "dataUrl": "data:application/octet-stream;base64," + base64.b64encode(b"x" * 50).decode()},
        ], "x")
        print("FAIL: expected AttachmentError on oversized file")
        return 1
    except att.AttachmentError as e:
        assert "exceeds" in str(e).lower() or "bytes" in str(e).lower()
        print("PASS: oversized attachment raises AttachmentError")
    finally:
        att.MAX_FILE_BYTES = 12 * 1024 * 1024

    # 6. missing dataUrl
    try:
        att.materialize([{"filename": "x", "isImage": True}], "x")
        print("FAIL: expected error on missing dataUrl")
        return 1
    except att.AttachmentError:
        print("PASS: missing dataUrl raises")

    # 7. user_message_for_store
    s = att.user_message_for_store("hi", mats)
    assert "dot.png" in s and "hi" in s
    print("PASS: store text mentions attachments")

    # 8. supports_native_vision
    assert att.supports_native_vision("grok")
    assert att.supports_native_vision("claude-code")
    assert not att.supports_native_vision("qwen3-next-80b")
    print("PASS: native vision model set")

    # 9. grok _cmd: plain -p vs multimodal --prompt-file (never fat --prompt-json argv)
    from argus import grok_code as gc
    s = gc.GrokSession("cmd-test")
    s.session_id = "00000000-0000-0000-0000-000000000001"
    cmd = s._cmd(prompt="hi")
    assert "-p" in cmd and "--prompt-file" not in cmd
    cmd2 = s._cmd(prompt_file="/tmp/argus-fake-prompt.json")
    assert "--prompt-file" in cmd2 and "-p" not in cmd2
    print("PASS: grok _cmd -p vs --prompt-file")

    print("\nALL ATTACHMENT TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
