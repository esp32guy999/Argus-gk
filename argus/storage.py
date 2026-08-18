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
import os
import sqlite3
import threading
import time

from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart,
)

from . import metrics

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
    content         TEXT NOT NULL,
    client_msg_id   TEXT             -- PWA idempotency key; unique per conversation
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
-- idx_messages_client_msg is created in _migrate() AFTER the column exists
-- on old DBs (CREATE TABLE IF NOT EXISTS won't add client_msg_id).

-- Per-thread roster (specs/chat-client.md). Messages stay the history;
-- this is who is IN the room and who the next send is addressed to.
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    participants    TEXT NOT NULL DEFAULT '[]',  -- JSON [model_id, ...]
    addressed       TEXT NOT NULL DEFAULT '[]',  -- JSON [model_id, ...]
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL
);

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

-- Supervisory Execution Engine (SEE) — durable task objects (specs/see.md).
-- Full JSON blob is source of truth; state columns support listing/filters.
CREATE TABLE IF NOT EXISTS see_tasks (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    state           TEXT NOT NULL,
    importance      INTEGER NOT NULL DEFAULT 50,
    conversation_id TEXT,
    job_id          TEXT,
    payload         TEXT NOT NULL,       -- full SeeTask JSON
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_see_tasks_state ON see_tasks(state, updated_ts);
CREATE INDEX IF NOT EXISTS idx_see_tasks_conv ON see_tasks(conversation_id, updated_ts);

CREATE TABLE IF NOT EXISTS see_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    ts              REAL NOT NULL,
    type            TEXT NOT NULL,
    detail          TEXT,
    data            TEXT,               -- JSON
    FOREIGN KEY (task_id) REFERENCES see_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_see_events_task ON see_events(task_id, id);

-- Memory-candidate ledger (research-lane D5). Append-only. During real usage the
-- agent flags moments that MIGHT be worth remembering — a dead-end query, a
-- correction, a repeated lookup — with a short reason. This is NOT memory: no
-- retrieval, no curation, no trust. It is the empirical dataset Task 3's write
-- policy will be designed FROM, so we don't design curation in a vacuum. Kept
-- deliberately dumb: collect now, decide what's worth keeping later.
CREATE TABLE IF NOT EXISTS memory_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    conversation_id TEXT,
    kind            TEXT NOT NULL,      -- config|personal|document|research|rule|… (see memory_policy)
    summary         TEXT NOT NULL,      -- one line: what happened
    detail          TEXT,               -- optional fuller context
    source          TEXT,               -- where it came from, e.g. 'web_fetch:<url>' | 'user'
    importance      INTEGER NOT NULL DEFAULT 50,  -- 0–100 future-Shane value (policy)
    policy          TEXT                -- 'tool'|'orchestrator'|'user' — who decided to flag
);
CREATE INDEX IF NOT EXISTS idx_memcand_ts ON memory_candidates(ts);
-- idx_memcand_importance created in _migrate after ALTER adds columns on old DBs

-- Long-term memory facts with an explicit LIFECYCLE state machine (Task 3, per
-- specs/memory_system.md §6a). A fact is never destructively deleted: superseded and
-- invalidated rows stay for decision-provenance/audit. Confirmation requires a TRUSTED
-- signal (§6b) — repetition can't confirm. Thresholds for promotion are NOT encoded
-- here; they're data-derived (§11). This is the evidence-independent mechanism only.
CREATE TABLE IF NOT EXISTS memory_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL,       -- what the fact is about, e.g. 'printer.ip'
    value           TEXT NOT NULL,       -- the claim
    state           TEXT NOT NULL,       -- proposed|observed|confirmed|superseded|invalidated
    source          TEXT,                -- origin: 'user'|'tool'|'web_fetch:<url>'|model id
    trust           REAL NOT NULL DEFAULT 0,   -- 0..1, evolves with state; tuned from data later
    mention_count   INTEGER NOT NULL DEFAULT 0, -- observed N times (does NOT auto-confirm)
    superseded_by   INTEGER,             -- fact id that replaced this one, if any
    conversation_id TEXT,
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_state ON memory_facts(state, updated_ts);
CREATE INDEX IF NOT EXISTS idx_facts_key ON memory_facts(key);

-- Append-only audit of every state transition (§7). Nothing about a fact changes
-- without a row here, so consolidation is a REVERSIBLE transformation: replay the log.
CREATE TABLE IF NOT EXISTS consolidation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    fact_id     INTEGER NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL,
    signal      TEXT                     -- what justified it: 'user'|'tool_groundtruth'|'multi_source'|'mention'|...
);
CREATE INDEX IF NOT EXISTS idx_conslog_fact ON consolidation_log(fact_id, id);
"""

# Keep in sync with argus.memory_policy.MEMORY_KINDS (legacy + flywheel kinds).
MEMORY_CANDIDATE_KINDS = (
    "dead_end", "correction", "repeat_lookup", "rule", "other",
    "config", "personal", "document", "research", "event",
)

# ── Memory fact lifecycle (state machine) ───────────────────────────────
FACT_STATES = ("proposed", "observed", "confirmed", "superseded", "invalidated")
# History states: retained for audit, hidden from active recall.
FACT_HISTORY_STATES = ("superseded", "invalidated")
# Allowed transitions. invalidated is terminal (kept immutable for audit).
_FACT_TRANSITIONS: dict[str, set[str]] = {
    "proposed":    {"observed", "confirmed", "superseded", "invalidated"},
    "observed":    {"confirmed", "superseded", "invalidated"},
    "confirmed":   {"superseded", "invalidated"},
    "superseded":  {"invalidated"},
    "invalidated": set(),
}
# Only these signals may promote a fact to 'confirmed'. A mention count is NOT here —
# that's the poisoning-by-repetition defense (§6b): repetition can never confirm.
TRUSTED_SIGNALS = ("user", "tool_groundtruth", "multi_source")

# Columns added after the table first shipped; ALTER-migrated onto live dbs in _migrate().
_JOB_MIGRATIONS = {
    "category": "ALTER TABLE jobs ADD COLUMN category TEXT NOT NULL DEFAULT 'external'",
    "next_check_ts": "ALTER TABLE jobs ADD COLUMN next_check_ts REAL",
}
_MEMCAND_MIGRATIONS = {
    "importance": "ALTER TABLE memory_candidates ADD COLUMN importance INTEGER NOT NULL DEFAULT 50",
    "policy": "ALTER TABLE memory_candidates ADD COLUMN policy TEXT",
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
        """Additively bring existing tables up to the current schema —
        SQLite CREATE TABLE IF NOT EXISTS never adds columns to an existing table."""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        for col, ddl in _JOB_MIGRATIONS.items():
            if col not in have:
                self._conn.execute(ddl)
        have_mc = {r[1] for r in self._conn.execute("PRAGMA table_info(memory_candidates)")}
        for col, ddl in _MEMCAND_MIGRATIONS.items():
            if col not in have_mc:
                self._conn.execute(ddl)
        # Index on importance only after the column exists (old DBs: after ALTER).
        have_mc = {r[1] for r in self._conn.execute("PRAGMA table_info(memory_candidates)")}
        if "importance" in have_mc:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memcand_importance "
                "ON memory_candidates(importance, ts)")
        have_msg = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "client_msg_id" not in have_msg:
            self._conn.execute("ALTER TABLE messages ADD COLUMN client_msg_id TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_msg "
            "ON messages(conversation_id, client_msg_id) "
            "WHERE client_msg_id IS NOT NULL")
        # conversations table is CREATE IF NOT EXISTS in _SCHEMA.

    def _fmt_msg(self, r) -> dict:
        d = dict(r)
        d["timestamp"] = round(d["ts"] * 1000)
        return d

    @staticmethod
    def _json_ids(raw) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(x) for x in raw if x]
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if x]

    # --- writes -----------------------------------------------------------
    def add_message(self, conversation_id: str, role: str, content: str,
                    model: str | None = None,
                    client_msg_id: str | None = None) -> dict:
        if client_msg_id:
            existing = self.get_by_client_msg_id(conversation_id, client_msg_id)
            if existing:
                return existing
        ts = time.time()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO messages (conversation_id, ts, role, model, content, client_msg_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, ts, role, model, content, client_msg_id),
                )
                self._conn.commit()
                mid = cur.lastrowid
            except sqlite3.IntegrityError:
                self._conn.rollback()
                r = self._conn.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
                    (conversation_id, client_msg_id),
                ).fetchone()
                if r:
                    return self._fmt_msg(r)
                raise
        return {"id": mid, "conversation_id": conversation_id, "ts": ts,
                "role": role, "model": model, "content": content,
                "client_msg_id": client_msg_id, "timestamp": round(ts * 1000)}

    def get_by_client_msg_id(self, conversation_id: str, client_msg_id: str) -> dict | None:
        """User row stamped with this PWA idempotency key, or None."""
        if not client_msg_id:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND client_msg_id = ?",
                (conversation_id, client_msg_id),
            ).fetchone()
        return self._fmt_msg(r) if r else None

    def replies_for_user(self, conversation_id: str, user_id: int) -> list[dict]:
        """Assistant rows after this user message, until the next user row."""
        with self._lock:
            nxt = self._conn.execute(
                "SELECT MIN(id) n FROM messages WHERE conversation_id = ? "
                "AND role = 'user' AND id > ?",
                (conversation_id, user_id),
            ).fetchone()
            q = ("SELECT * FROM messages WHERE conversation_id = ? "
                 "AND role = 'assistant' AND id > ?")
            args: list = [conversation_id, user_id]
            if nxt and nxt["n"] is not None:
                q += " AND id < ?"
                args.append(nxt["n"])
            q += " ORDER BY id ASC"
            rows = self._conn.execute(q, args).fetchall()
        return [self._fmt_msg(r) for r in rows]

    def resolve_chat_send(self, conversation_id: str, client_msg_id: str | None,
                          to: list[str], *, turn_active: bool = False,
                          turn_client_msg_id: str | None = None) -> dict:
        """Idempotency table from specs/chat-client.md.

        action:
          new           — persist a user row and generate every `to` model
          replay_active — same id, turn still running; do not persist or generate
          replay_done   — same id, every `to` model already replied
          resume        — user row exists; generate only `missing` models
        """
        targets = [m for m in to if m]
        if not client_msg_id:
            return {"action": "new", "user": None, "missing": targets}
        user = self.get_by_client_msg_id(conversation_id, client_msg_id)
        if not user:
            return {"action": "new", "user": None, "missing": targets}
        if turn_active and turn_client_msg_id == client_msg_id:
            return {"action": "replay_active", "user": user, "missing": []}
        have = {r.get("model") for r in self.replies_for_user(conversation_id, user["id"])}
        missing = [m for m in targets if m not in have]
        if not missing:
            return {"action": "replay_done", "user": user, "missing": []}
        return {"action": "resume", "user": user, "missing": missing}

    def add_memory_candidate(self, kind: str, summary: str, *,
                             conversation_id: str | None = None,
                             detail: str | None = None,
                             source: str | None = None,
                             importance: int = 50,
                             policy: str | None = None) -> dict:
        """Append a memory-worthy candidate (research-lane D5). Append-only; never
        updated or trusted — just collected for Task 3's future write policy.
        Prefer argus.memory_policy.accept_candidate() so importance is scored."""
        if kind not in MEMORY_CANDIDATE_KINDS:
            kind = "other"
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 50
        importance = max(0, min(100, importance))
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_candidates (ts, conversation_id, kind, summary, "
                "detail, source, importance, policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, conversation_id, kind, summary, detail, source, importance, policy),
            )
            self._conn.commit()
            cid = cur.lastrowid
        return {"id": cid, "ts": ts, "conversation_id": conversation_id,
                "kind": kind, "summary": summary, "detail": detail, "source": source,
                "importance": importance, "policy": policy}

    def list_memory_candidates(self, *, limit: int = 100, kind: str | None = None) -> list[dict]:
        """Most-recent-first. Read-only view for triage — no scoring, no dedup."""
        q = "SELECT * FROM memory_candidates"
        args: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        q += " ORDER BY id DESC LIMIT ?"
        args += (limit,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ── Memory facts + lifecycle state machine (Task 3 mechanism) ──────────
    def add_fact(self, key: str, value: str, *, source: str | None = None,
                 trust: float = 0.0, conversation_id: str | None = None) -> dict:
        """Create a fact in the 'proposed' state. Promotion happens only via
        transition_fact() — never here, and never by mere repetition (§6b)."""
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_facts (key, value, state, source, trust, "
                "mention_count, conversation_id, created_ts, updated_ts) "
                "VALUES (?, ?, 'proposed', ?, ?, 0, ?, ?, ?)",
                (key, value, source, trust, conversation_id, ts, ts),
            )
            self._conn.commit()
            fid = cur.lastrowid
        return self.get_fact(fid)

    def get_fact(self, fact_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM memory_facts WHERE id = ?",
                                    (fact_id,)).fetchone()
        return dict(r) if r else None

    def observe_fact(self, fact_id: int, *, reason: str = "observed") -> dict | None:
        """Record that a fact was seen/used again. Increments mention_count and, on the
        FIRST observation, moves proposed→observed. It NEVER reaches 'confirmed' — a
        mention count is not a trusted signal (poisoning-by-repetition defense §6b)."""
        f = self.get_fact(fact_id)
        if f is None:
            raise ValueError(f"no such fact {fact_id}")
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE memory_facts SET mention_count = mention_count + 1, "
                "updated_ts = ? WHERE id = ?", (ts, fact_id))
            self._conn.commit()
        if f["state"] == "proposed":
            return self.transition_fact(fact_id, "observed", reason=reason, signal="mention")
        return self.get_fact(fact_id)

    def transition_fact(self, fact_id: int, to_state: str, *, reason: str,
                        signal: str | None = None) -> dict:
        """Move a fact through its lifecycle, enforcing the state machine and logging
        the transition to consolidation_log (append-only, reversible §7).

        HARD RULE (§6b): promotion to 'confirmed' requires a TRUSTED signal
        (user / tool_groundtruth / multi_source). Repetition / 'mention' can't confirm."""
        if to_state not in FACT_STATES:
            raise ValueError(f"unknown state {to_state!r}")
        f = self.get_fact(fact_id)
        if f is None:
            raise ValueError(f"no such fact {fact_id}")
        frm = f["state"]
        if to_state not in _FACT_TRANSITIONS.get(frm, set()):
            raise ValueError(f"illegal transition {frm} -> {to_state}")
        if to_state == "confirmed" and signal not in TRUSTED_SIGNALS:
            raise ValueError(
                f"confirmation requires a trusted signal {TRUSTED_SIGNALS}, got {signal!r} "
                f"(repetition/mention cannot confirm — poisoning defense)")
        if not reason:
            raise ValueError("a transition must record a reason (audit §7)")
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO consolidation_log (ts, fact_id, from_state, to_state, "
                "reason, signal) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, fact_id, frm, to_state, reason, signal))
            self._conn.execute(
                "UPDATE memory_facts SET state = ?, updated_ts = ? WHERE id = ?",
                (to_state, ts, fact_id))
            self._conn.commit()
        try:
            metrics.MEMORY_TRANSITIONS.labels(to_state).inc()
        except Exception:
            pass
        return self.get_fact(fact_id)

    def list_facts(self, *, state: str | None = None,
                   include_history: bool = True) -> list[dict]:
        """Facts, newest-updated first. include_history=False hides superseded/
        invalidated rows (kept for audit, but off the active-recall path §6a)."""
        q = "SELECT * FROM memory_facts"
        clauses, args = [], []
        if state:
            clauses.append("state = ?"); args.append(state)
        elif not include_history:
            qs = ",".join("?" * len(FACT_HISTORY_STATES))
            clauses.append(f"state NOT IN ({qs})"); args.extend(FACT_HISTORY_STATES)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY updated_ts DESC"
        with self._lock:
            rows = self._conn.execute(q, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def consolidation_log(self, *, fact_id: int | None = None, limit: int = 500) -> list[dict]:
        """Append-only transition history. Filter by fact for its full audit trail."""
        q = "SELECT * FROM consolidation_log"
        args: tuple = ()
        if fact_id is not None:
            q += " WHERE fact_id = ?"; args = (fact_id,)
        q += " ORDER BY id DESC LIMIT ?"
        args += (limit,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def delete_message(self, message_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._conn.commit()

    def clear(self, conversation_id: str | None = None) -> None:
        with self._lock:
            if conversation_id is None:
                self._conn.execute("DELETE FROM messages")
                self._conn.execute("DELETE FROM conversations")
            else:
                self._conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                self._conn.execute(
                    "DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self._conn.commit()

    # --- reads ------------------------------------------------------------
    def get_messages(self, conversation_id: str, limit: int = 100,
                     before_id: int | None = None,
                     after_id: int | None = None) -> list[dict]:
        """Page of rows, returned OLDEST-first.

        `before_id` pages older (newest `limit` rows with id < before_id).
        `after_id` pages newer (oldest `limit` rows with id > after_id).
        Both can be combined as a range.
        """
        q = "SELECT * FROM messages WHERE conversation_id = ?"
        args: list = [conversation_id]
        if after_id is not None:
            q += " AND id > ?"
            args.append(after_id)
        if before_id is not None:
            q += " AND id < ?"
            args.append(before_id)
        if after_id is not None:
            q += " ORDER BY id ASC LIMIT ?"
            args.append(limit)
            with self._lock:
                rows = self._conn.execute(q, args).fetchall()
            return [self._fmt_msg(r) for r in rows]
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._fmt_msg(r) for r in reversed(rows)]

    def _infer_participants(self, conversation_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT model FROM messages WHERE conversation_id = ? "
            "AND role = 'assistant' AND model IS NOT NULL AND model != '' "
            "GROUP BY model ORDER BY MIN(id)",
            (conversation_id,),
        ).fetchall()
        from argus.chat_client import is_inviteable
        return [r["model"] for r in rows if r["model"] and is_inviteable(r["model"])]

    def _last_assistant_model(self, conversation_id: str) -> str | None:
        r = self._conn.execute(
            "SELECT model FROM messages WHERE conversation_id = ? "
            "AND role = 'assistant' AND model IS NOT NULL AND model != '' "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return r["model"] if r else None

    def _fmt_conversation(self, cid: str, *, title: str | None, count: int,
                          last_ts: float | None, participants: list[str],
                          addressed: list[str]) -> dict:
        return {
            "id": cid,
            "title": title or cid,
            "count": count,
            "last_ts": last_ts,
            "participants": list(participants),
            "addressed": list(addressed),
        }

    def get_conversation(self, conversation_id: str) -> dict | None:
        """One thread + roster. Infers participants from assistant models if
        the thread has no roster row yet."""
        with self._lock:
            stats = self._conn.execute(
                "SELECT COUNT(*) n, MAX(ts) last_ts FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            first = self._conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND role='user' "
                "ORDER BY id ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            roster = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            inferred = self._infer_participants(conversation_id)
            last_asst = self._last_assistant_model(conversation_id)
        count = int(stats["n"] or 0) if stats else 0
        last_ts = stats["last_ts"] if stats else None
        if roster is None and count == 0:
            return None
        title = None
        participants: list[str] = []
        addressed: list[str] = []
        if roster is not None:
            title = roster["title"]
            participants = self._json_ids(roster["participants"])
            addressed = self._json_ids(roster["addressed"])
            if last_ts is None:
                last_ts = roster["updated_ts"]
        if first and first["content"]:
            title = first["content"][:60]
        if not participants:
            participants = inferred
        if not addressed:
            # Last speaker, not first-ever — otherwise an old 80B reply
            # hijacks the room and llama-swap evicts whatever was loaded.
            if last_asst and last_asst in (participants or inferred or [last_asst]):
                addressed = [last_asst]
            else:
                addressed = list(participants[:1])
        return self._fmt_conversation(
            conversation_id, title=title, count=count, last_ts=last_ts,
            participants=participants, addressed=addressed)

    def ensure_conversation(self, conversation_id: str, *, title: str | None = None,
                            participants: list[str] | None = None,
                            addressed: list[str] | None = None) -> dict:
        """Insert a roster row if missing; optionally seed title/roster."""
        now = time.time()
        parts = list(participants or [])
        addr = list(addressed or [])
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO conversations "
                    "(id, title, participants, addressed, created_ts, updated_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, title, json.dumps(parts), json.dumps(addr), now, now),
                )
                self._conn.commit()
        if participants is not None or addressed is not None or title is not None:
            if existing is not None:
                return self.update_conversation(
                    conversation_id, title=title, participants=participants,
                    addressed=addressed)
        got = self.get_conversation(conversation_id)
        return got or self._fmt_conversation(
            conversation_id, title=title, count=0, last_ts=now,
            participants=parts, addressed=addr)

    def update_conversation(self, conversation_id: str, *, title: str | None = None,
                            participants: list[str] | None = None,
                            addressed: list[str] | None = None) -> dict:
        """Patch roster. Removing a participant drops them from addressed."""
        self.ensure_conversation(conversation_id)
        cur = self.get_conversation(conversation_id) or {
            "title": None, "participants": [], "addressed": [],
        }
        if title is not None:
            cur["title"] = title
        if participants is not None:
            # de-dupe, keep order
            seen: set[str] = set()
            parts: list[str] = []
            for m in participants:
                if m and m not in seen:
                    seen.add(m)
                    parts.append(m)
            cur["participants"] = parts
            cur["addressed"] = [a for a in (addressed if addressed is not None
                                            else cur["addressed"]) if a in seen]
        elif addressed is not None:
            allow = set(cur["participants"])
            cur["addressed"] = [a for a in addressed if not allow or a in allow]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET title=?, participants=?, addressed=?, updated_ts=? "
                "WHERE id=?",
                (cur.get("title"), json.dumps(cur["participants"]),
                 json.dumps(cur["addressed"]), now, conversation_id),
            )
            self._conn.commit()
        return self.get_conversation(conversation_id) or cur

    def add_participants(self, conversation_id: str, models: list[str],
                         *, addressed: list[str] | None = None) -> dict:
        """Join models to the room. Optionally replace the addressee list."""
        conv = self.ensure_conversation(conversation_id)
        parts = list(conv["participants"])
        for m in models:
            if m and m not in parts:
                parts.append(m)
        return self.update_conversation(
            conversation_id, participants=parts, addressed=addressed)

    def list_conversations(self) -> list[dict]:
        """One entry per conversation: id, a title from the first user message,
        message count, last-activity ts, plus roster — newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT conversation_id, COUNT(*) n, MAX(ts) last_ts "
                "FROM messages GROUP BY conversation_id ORDER BY last_ts DESC"
            ).fetchall()
            firsts = {}
            inferred = {}
            for r in rows:
                cid = r["conversation_id"]
                t = self._conn.execute(
                    "SELECT content FROM messages WHERE conversation_id = ? AND role='user' "
                    "ORDER BY id ASC LIMIT 1", (cid,)
                ).fetchone()
                firsts[cid] = (t["content"][:60] if t else cid)
                inferred[cid] = self._infer_participants(cid)
            rosters = {r["id"]: r for r in self._conn.execute(
                "SELECT * FROM conversations").fetchall()}

        by_id: dict[str, dict] = {}
        for r in rows:
            cid = r["conversation_id"]
            roster = rosters.get(cid)
            parts = self._json_ids(roster["participants"]) if roster else []
            addr = self._json_ids(roster["addressed"]) if roster else []
            if not parts:
                parts = inferred.get(cid) or []
            if not addr:
                addr = list(parts[:1])
            by_id[cid] = self._fmt_conversation(
                cid, title=firsts.get(cid), count=r["n"], last_ts=r["last_ts"],
                participants=parts, addressed=addr)
        for cid, roster in rosters.items():
            if cid in by_id:
                continue
            by_id[cid] = self._fmt_conversation(
                cid, title=roster["title"] or cid, count=0,
                last_ts=roster["updated_ts"],
                participants=self._json_ids(roster["participants"]),
                addressed=self._json_ids(roster["addressed"]))
        return sorted(by_id.values(), key=lambda c: c["last_ts"] or 0, reverse=True)

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


    # ── SEE tasks (Supervisory Execution Engine) ───────────────────────────
    def save_see_task(self, task_dict: dict) -> dict:
        """Upsert a full SeeTask JSON blob + denormalized state columns."""
        tid = task_dict["id"]
        now = time.time()
        payload = json.dumps(task_dict)
        with self._lock:
            self._conn.execute(
                "INSERT INTO see_tasks (id, goal, state, importance, conversation_id, "
                "job_id, payload, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET goal=excluded.goal, state=excluded.state, "
                "importance=excluded.importance, conversation_id=excluded.conversation_id, "
                "job_id=excluded.job_id, payload=excluded.payload, "
                "updated_ts=excluded.updated_ts",
                (tid, task_dict.get("goal") or "", task_dict.get("current_state") or "NEW",
                 int(task_dict.get("importance") or 50),
                 task_dict.get("conversation_id"), task_dict.get("job_id"),
                 payload,
                 float(task_dict.get("created_ts") or now),
                 float(task_dict.get("updated_ts") or now)),
            )
            self._conn.commit()
        return task_dict

    def get_see_task(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT payload FROM see_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not r:
            return None
        try:
            return json.loads(r["payload"])
        except (TypeError, json.JSONDecodeError):
            return None

    def list_see_tasks(self, *, conversation_id: str | None = None,
                       include_terminal: bool = True, limit: int = 100) -> list[dict]:
        q = "SELECT payload, state FROM see_tasks WHERE 1=1"
        args: list = []
        if conversation_id is not None:
            q += " AND conversation_id=?"
            args.append(conversation_id)
        if not include_terminal:
            q += " AND state NOT IN ('COMPLETED','ABORTED')"
        q += " ORDER BY updated_ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except (TypeError, json.JSONDecodeError):
                continue
        return out

    def append_see_event(self, task_id: str, event: dict) -> None:
        """Append-only event row for replay (also kept inside task payload)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO see_events (task_id, ts, type, detail, data) VALUES (?,?,?,?,?)",
                (task_id, float(event.get("ts") or time.time()),
                 event.get("type") or "ToolCalled",
                 event.get("detail"),
                 json.dumps(event.get("data") or {})),
            )
            self._conn.commit()

    def list_see_events(self, task_id: str, *, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, type, detail, data FROM see_events WHERE task_id=? "
                "ORDER BY id ASC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            out.append({
                "ts": r["ts"], "type": r["type"], "detail": r["detail"], "data": data,
            })
        return out


# ── Process-wide Store singleton ──────────────────────────────────────────
# Tools are pure functions with no Store handle. This lets them reach the SAME
# DB the app opened (ui/server.py resolves it too), so a fact written by a tool is
# visible to the app. Path resolves once: explicit arg → $ARGUS_DB → 'argus.db'.
_STORE: "Store | None" = None
_STORE_LOCK = threading.Lock()


def get_store(path: str | None = None) -> "Store":
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = Store(path or os.environ.get("ARGUS_DB", "argus.db"))
    return _STORE
