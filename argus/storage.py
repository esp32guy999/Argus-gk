"""Conversation storage — the DA seam (SQLite now; Postgres+pgvector later behind
this same interface). Holds the chat history so the model has memory across turns
and the UI can page/delete it.

Reconstructs Pydantic AI message history from stored (role, content) rows as plain
alternating ModelRequest(user) / ModelResponse(text) messages. This deliberately
does NOT replay past tool calls — only the assistant's final text — which is what
conversational continuity needs and avoids coupling to pydantic-ai's internal
serialization format. Tool-call replay can be added later if a turn needs it.
"""
from __future__ import annotations
import sqlite3
import threading
import time

from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    ts              REAL NOT NULL,
    role            TEXT NOT NULL,   -- 'user' | 'assistant'
    model           TEXT,            -- model id for assistant rows, NULL for user
    content         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
"""


class Store:
    """Thread-safe SQLite conversation store. One process, low volume — a single
    connection guarded by a lock is plenty; WAL keeps reads/writes from blocking."""

    def __init__(self, path: str = "argus.db") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- writes -----------------------------------------------------------
    def add_message(self, conversation_id: str, role: str, content: str,
                    model: str | None = None) -> dict:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages (conversation_id, ts, role, model, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, ts, role, model, content),
            )
            self._conn.commit()
            mid = cur.lastrowid
        return {"id": mid, "conversation_id": conversation_id, "ts": ts,
                "role": role, "model": model, "content": content}

    def delete_message(self, message_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._conn.commit()

    def clear(self, conversation_id: str | None = None) -> None:
        with self._lock:
            if conversation_id is None:
                self._conn.execute("DELETE FROM messages")
            else:
                self._conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            self._conn.commit()

    # --- reads ------------------------------------------------------------
    def get_messages(self, conversation_id: str, limit: int = 100,
                     before_id: int | None = None) -> list[dict]:
        """Newest `limit` rows (optionally older than before_id), returned
        OLDEST-first to match the frontend's render order."""
        q = "SELECT * FROM messages WHERE conversation_id = ?"
        args: list = [conversation_id]
        if before_id is not None:
            q += " AND id < ?"
            args.append(before_id)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list_conversations(self) -> list[dict]:
        """One entry per conversation: id, a title from the first user message,
        message count, and last-activity ts — newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, COUNT(*) n, MAX(ts) last_ts "
                "FROM messages GROUP BY conversation_id ORDER BY last_ts DESC"
            ).fetchall()
            result = []
            for r in rows:
                t = self._conn.execute(
                    "SELECT content FROM messages WHERE conversation_id = ? AND role='user' "
                    "ORDER BY id ASC LIMIT 1", (r["conversation_id"],)
                ).fetchone()
                title = (t["content"][:60] if t else r["conversation_id"])
                result.append({"id": r["conversation_id"], "title": title,
                               "count": r["n"], "last_ts": r["last_ts"]})
        return result

    def model_history(self, conversation_id: str, limit: int = 20) -> list[ModelMessage]:
        """Reconstruct pydantic-ai message history (last `limit` rows) so the model
        sees prior turns. user -> ModelRequest, assistant -> ModelResponse(text)."""
        rows = self.get_messages(conversation_id, limit=limit)
        history: list[ModelMessage] = []
        for r in rows:
            if r["role"] == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=r["content"])]))
            else:
                history.append(ModelResponse(parts=[TextPart(content=r["content"])]))
        return history
