"""Contract test for the conversation Store (argus/storage.py).

Defines what the DA seam MUST do: persist turns, page oldest-first, reconstruct
pydantic-ai message history, delete, and list conversations. Offline, uses a temp
SQLite file. Runnable standalone:  python tests/test_storage.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus.storage import Store
    from pydantic_ai.messages import ModelRequest, ModelResponse

    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        s = Store(path)

        # 1. add + read back oldest-first
        s.add_message("c1", "user", "hello", None)
        s.add_message("c1", "assistant", "hi there", "m1")
        s.add_message("c1", "user", "bye", None)
        rows = s.get_messages("c1")
        assert [r["role"] for r in rows] == ["user", "assistant", "user"], rows
        assert rows[0]["content"] == "hello" and rows[1]["model"] == "m1", rows
        assert rows[0]["id"] < rows[2]["id"], "ids ascending (oldest first)"
        print("PASS: add + get_messages oldest-first")

        # 2. before_id paging returns only older rows
        older = s.get_messages("c1", before_id=rows[2]["id"])
        assert [r["content"] for r in older] == ["hello", "hi there"], older
        print("PASS: before_id paging")

        # 3. conversation isolation
        s.add_message("c2", "user", "other thread", None)
        assert len(s.get_messages("c1")) == 3 and len(s.get_messages("c2")) == 1
        print("PASS: conversations isolated by id")

        # 4. model_history -> alternating pydantic-ai messages in order
        hist = s.model_history("c1")
        assert len(hist) == 3, hist
        assert isinstance(hist[0], ModelRequest), type(hist[0])
        assert isinstance(hist[1], ModelResponse), type(hist[1])
        assert isinstance(hist[2], ModelRequest), type(hist[2])
        print("PASS: model_history reconstructs pydantic-ai messages")

        # 5. model_history limit keeps the most RECENT n (still chronological)
        hist2 = s.model_history("c1", limit=2)
        assert len(hist2) == 2 and isinstance(hist2[0], ModelResponse), hist2
        print("PASS: model_history limit keeps most-recent n in order")

        # 5b. model_history max_tokens budget: keep newest that fit, drop oldest,
        #     never exceed the budget (protects small-context models like the 80B).
        from argus.storage import est_tokens
        s.add_message("cb", "user", "A" * 700, None)      # ~200 tok (oldest)
        s.add_message("cb", "assistant", "B" * 350, "m")  # ~100 tok
        s.add_message("cb", "user", "C" * 350, None)      # ~100 tok (newest)
        budget = 150
        histb = s.model_history("cb", limit=20, max_tokens=budget)
        # newest (C, 100) fits; next (B, +100=200) would exceed 150 -> stop. Keep just C.
        assert len(histb) == 1, histb
        assert histb[0].parts[0].content.startswith("C"), "kept the NEWEST message"
        total = sum(est_tokens(p.content) for m in histb for p in m.parts)
        assert total <= budget, (total, budget)
        # bigger budget keeps more, still chronological + within budget
        histb2 = s.model_history("cb", limit=20, max_tokens=250)
        assert len(histb2) == 2 and histb2[0].parts[0].content.startswith("B"), histb2
        # no budget -> count-limited as before
        assert len(s.model_history("cb", limit=20)) == 3
        s.clear("cb")   # don't pollute the later list_conversations assertion
        print("PASS: model_history token budget keeps newest-that-fit, never overflows")

        # 6. list_conversations: one per id, title from first user msg, newest first
        convs = {c["id"]: c for c in s.list_conversations()}
        assert set(convs) == {"c1", "c2"}, convs
        assert convs["c1"]["title"] == "hello" and convs["c1"]["count"] == 3, convs["c1"]
        print("PASS: list_conversations")

        # 7. delete one message, then clear a thread
        s.delete_message(rows[2]["id"])
        assert len(s.get_messages("c1")) == 2, "deleted one row"
        s.clear("c1")
        assert s.get_messages("c1") == [] and len(s.get_messages("c2")) == 1, "cleared c1 only"
        print("PASS: delete_message + clear(thread)")

        # 8. client_msg_id is idempotent per conversation
        first = s.add_message("c3", "user", "once", None, client_msg_id="abc")
        again = s.add_message("c3", "user", "twice", None, client_msg_id="abc")
        assert first["id"] == again["id"], (first, again)
        assert len(s.get_messages("c3")) == 1
        other = s.add_message("c4", "user", "other room", None, client_msg_id="abc")
        assert other["id"] != first["id"]
        print("PASS: client_msg_id idempotent per conversation")

        # 9. after_id pages newer rows oldest-first
        a = s.add_message("c5", "user", "a")
        b = s.add_message("c5", "assistant", "b", "m1")
        c = s.add_message("c5", "user", "c")
        newer = s.get_messages("c5", after_id=a["id"])
        assert [r["content"] for r in newer] == ["b", "c"], newer
        none = s.get_messages("c5", after_id=c["id"])
        assert none == []
        print("PASS: after_id paging")

        # 10. roster + infer + resolve_chat_send
        s.ensure_conversation("c6", participants=["m1"], addressed=["m1"])
        u = s.add_message("c6", "user", "hi", None, client_msg_id="u1")
        s.add_message("c6", "assistant", "yo", "m1")
        conv = s.get_conversation("c6")
        assert conv["participants"] == ["m1"] and conv["addressed"] == ["m1"], conv
        listed = {x["id"]: x for x in s.list_conversations()}
        assert listed["c6"]["participants"] == ["m1"], listed["c6"]
        # infer when no roster
        s.add_message("c7", "user", "hey")
        s.add_message("c7", "assistant", "ok", "gemma4-26b")
        s.add_message("c7", "assistant", "ok2", "grok")
        infer = s.get_conversation("c7")
        assert infer["participants"] == ["gemma4-26b", "grok"], infer
        # idempotency table
        r_new = s.resolve_chat_send("c6", None, ["m1"])
        assert r_new["action"] == "new" and r_new["missing"] == ["m1"]
        r_act = s.resolve_chat_send("c6", "u1", ["m1"], turn_active=True,
                                    turn_client_msg_id="u1")
        assert r_act["action"] == "replay_active" and r_act["user"]["id"] == u["id"]
        r_done = s.resolve_chat_send("c6", "u1", ["m1"])
        assert r_done["action"] == "replay_done"
        r_res = s.resolve_chat_send("c6", "u1", ["m1", "grok"])
        assert r_res["action"] == "resume" and r_res["missing"] == ["grok"], r_res
        # remove participant drops addressed
        s.add_participants("c6", ["grok"], addressed=["m1", "grok"])
        patched = s.update_conversation("c6", participants=["grok"])
        assert patched["participants"] == ["grok"] and patched["addressed"] == ["grok"], patched
        print("PASS: roster + resolve_chat_send")

        print("\nALL STORAGE CONTRACT TESTS PASSED")
        return 0
    finally:
        for ext in ("", "-wal", "-shm"):
            try: os.unlink(path + ext)
            except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
