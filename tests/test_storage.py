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

        print("\nALL STORAGE CONTRACT TESTS PASSED")
        return 0
    finally:
        for ext in ("", "-wal", "-shm"):
            try: os.unlink(path + ext)
            except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
