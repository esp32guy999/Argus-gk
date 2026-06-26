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
import json
import sqlite3
import threading
import time

from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart,
)

def est_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate: ~3.5 chars/token, rounded up.
    Used to budget history against a model's context window without a real
    tokenizer. Slightly conservative — better to trim a little extra than to
    overflow a small window (e.g. the local 80B's 8192)."""
    n = len(text or "")
    return max(1, (n * 2 + 6) // 7)   # ceil(n / 3.5)


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

-- The Work Ledger: durable state for every long-running thing in flight, so it
-- survives session resets and the reconciler can advance/notify without a human
-- poking. See docs/DESIGN-work-ledger.md. A job is only ever 'done' because its
-- probe returned truth — never because the model claimed it.
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,   -- short slug, e.g. 'dl-leftover-salmon'
    kind            TEXT NOT NULL,      -- 'download'|'bakeoff'|'generation'|'import'|'deploy'|'task'
    category        TEXT NOT NULL DEFAULT 'external',  -- 'agent' (I act) | 'external' (a probe watches)
    title           TEXT NOT NULL,      -- human label for the board
    state           TEXT NOT NULL,      -- proposed|queued|active|blocked|done|failed|cancelled
    progress        REAL,               -- 0.0-1.0, NULL = indeterminate
    detail          TEXT,               -- last status line / error
    probe           TEXT,               -- JSON: how the reconciler refreshes truth
    payload         TEXT,               -- JSON: kind-specific data (incl. proposed plan steps)
    next_check_ts   REAL,               -- external lane: when to next probe (ETA-adaptive)
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL,
    done_ts         REAL,
    conversation_id TEXT                 -- which chat to notify on transition
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, updated_ts);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(category, next_check_ts);
"""

# Columns added after the table first shipped; ALTER-migrated onto live dbs in _migrate().
_JOB_MIGRATIONS = {
    "category": "ALTER TABLE jobs ADD COLUMN category TEXT NOT NULL DEFAULT 'external'",
    "next_check_ts": "ALTER TABLE jobs ADD COLUMN next_check_ts REAL",
}

# Job lifecycle. Terminal states are skipped by the reconciler.
JOB_STATES = ("proposed", "queued", "active", "blocked", "done", "failed", "cancelled")
JOB_TERMINAL = ("done", "failed", "cancelled")
JOB_CATEGORIES = ("agent", "external")


class Store:
    """Thread-safe SQLite conversation store. One process, low volume — a single
    connection guarded by a lock is plenty; WAL keeps reads/writes from blocking."""

    def __init__(self, path: str = "argus.db") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additively bring an existing `jobs` table up to the current schema —
        SQLite CREATE TABLE IF NOT EXISTS never adds columns to an existing table."""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, ddl in _JOB_MIGRATIONS.items():
            if col not in have:
                self._conn.execute(ddl)

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
        out = []
        for r in reversed(rows):
            d = dict(r)
            # Frontend reads `timestamp` and does new Date(...), which wants ms.
            d["timestamp"] = round(d["ts"] * 1000)
            out.append(d)
        return out

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

    def model_history(self, conversation_id: str, limit: int = 20,
                      max_tokens: int | None = None) -> list[ModelMessage]:
        """Reconstruct pydantic-ai message history so the model sees prior turns.
        user -> ModelRequest, assistant -> ModelResponse(text).

        `limit` caps by message COUNT (newest rows). `max_tokens`, if given, also
        caps by estimated TOKENS: keep the newest messages that fit the budget and
        drop the oldest — so a small-context model (e.g. the 80B at 8192) can't be
        overflowed by long/tool-heavy history. May keep zero if even the newest
        message exceeds the budget (safe: the live prompt is sent separately)."""
        rows = self.get_messages(conversation_id, limit=limit)   # oldest-first
        if max_tokens is not None:
            kept: list[dict] = []
            used = 0
            for r in reversed(rows):                              # newest-first
                cost = est_tokens(r["content"])
                if used + cost > max_tokens:
                    break
                used += cost
                kept.append(r)
            rows = list(reversed(kept))                           # back to oldest-first
        history: list[ModelMessage] = []
        for r in rows:
            if r["role"] == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=r["content"])]))
            else:
                history.append(ModelResponse(parts=[TextPart(content=r["content"])]))
        return history

    # --- the work ledger --------------------------------------------------
    @staticmethod
    def _job_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("probe", "payload"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    pass
        return d

    def add_job(self, id: str, kind: str, title: str, *, state: str = "queued",
                category: str = "external", progress: float | None = None,
                detail: str | None = None, probe: dict | None = None,
                payload: dict | None = None, next_check_ts: float | None = None,
                conversation_id: str | None = None) -> dict:
        """Register (or replace) a ledger job. INSERT OR REPLACE so re-registering
        an id is idempotent — handy for jobs migrated in on startup. `category`
        decides which loop owns it: 'external' (a probe watches) or 'agent' (I act)."""
        if state not in JOB_STATES:
            raise ValueError(f"bad job state: {state!r}")
        if category not in JOB_CATEGORIES:
            raise ValueError(f"bad job category: {category!r}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs "
                "(id, kind, category, title, state, progress, detail, probe, payload, "
                " next_check_ts, created_ts, updated_ts, done_ts, conversation_id) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                " COALESCE((SELECT created_ts FROM jobs WHERE id=?), ?), ?, ?, ?)",
                (id, kind, category, title, state, progress, detail,
                 json.dumps(probe) if probe else None,
                 json.dumps(payload) if payload else None,
                 next_check_ts, id, now, now,
                 now if state in JOB_TERMINAL else None, conversation_id),
            )
            self._conn.commit()
        return self.get_job(id)

    def update_job(self, id: str, **fields) -> dict | None:
        """Patch a job. Accepts state/progress/detail/probe/payload/title.
        Stamps updated_ts, and done_ts when transitioning to a terminal state.
        Returns the fresh row, or None if the id is unknown."""
        cur = self.get_job(id)
        if cur is None:
            return None
        cols, args = [], []
        for k in ("state", "category", "progress", "detail", "title", "next_check_ts"):
            if k in fields:
                if k == "state" and fields[k] not in JOB_STATES:
                    raise ValueError(f"bad job state: {fields[k]!r}")
                if k == "category" and fields[k] not in JOB_CATEGORIES:
                    raise ValueError(f"bad job category: {fields[k]!r}")
                cols.append(f"{k}=?"); args.append(fields[k])
        for k in ("probe", "payload"):
            if k in fields:
                v = fields[k]
                cols.append(f"{k}=?")
                args.append(json.dumps(v) if v is not None else None)
        now = time.time()
        cols.append("updated_ts=?"); args.append(now)
        if fields.get("state") in JOB_TERMINAL:
            cols.append("done_ts=?"); args.append(now)
        args.append(id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", args)
            self._conn.commit()
        return self.get_job(id)

    def get_job(self, id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        return self._job_row(r) if r else None

    def list_jobs(self, *, include_terminal: bool = True,
                  limit: int = 200) -> list[dict]:
        """Board order: live work first (active/blocked/queued/proposed), then the
        most recently finished. Terminal jobs can be hidden with include_terminal=False."""
        q = "SELECT * FROM jobs"
        if not include_terminal:
            q += " WHERE state NOT IN ('done','failed','cancelled')"
        # CASE orders live states above terminal ones; then newest activity first.
        q += (" ORDER BY CASE state "
              "WHEN 'active' THEN 0 WHEN 'blocked' THEN 1 WHEN 'queued' THEN 2 "
              "WHEN 'proposed' THEN 3 ELSE 4 END, updated_ts DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(q, (limit,)).fetchall()
        return [self._job_row(r) for r in rows]

    def due_jobs(self, now: float, *, category: str = "external") -> list[dict]:
        """Non-terminal jobs of a category that are due for a probe — next_check_ts
        is NULL (never scheduled) or in the past. Drives the ETA-adaptive scheduler."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE category=? "
                "AND state NOT IN ('done','failed','cancelled','proposed') "
                "AND (next_check_ts IS NULL OR next_check_ts <= ?) "
                "ORDER BY next_check_ts IS NULL DESC, next_check_ts ASC",
                (category, now),
            ).fetchall()
        return [self._job_row(r) for r in rows]

    def next_due_ts(self, *, category: str = "external") -> float | None:
        """Soonest next_check_ts among schedulable jobs, so the loop can sleep
        exactly until the next one is due. None if nothing is scheduled."""
        with self._lock:
            r = self._conn.execute(
                "SELECT MIN(next_check_ts) m FROM jobs WHERE category=? "
                "AND state NOT IN ('done','failed','cancelled','proposed') "
                "AND next_check_ts IS NOT NULL",
                (category,),
            ).fetchone()
        return r["m"] if r and r["m"] is not None else None
