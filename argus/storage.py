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
    kind            TEXT NOT NULL,      -- 'dead_end'|'correction'|'repeat_lookup'|'rule'|'other'
    summary         TEXT NOT NULL,      -- one line: what happened
    detail          TEXT,               -- optional fuller context
    source          TEXT                -- where it came from, e.g. 'web_fetch:<url>' | 'user'
);
CREATE INDEX IF NOT EXISTS idx_memcand_ts ON memory_candidates(ts);

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

MEMORY_CANDIDATE_KINDS = ("dead_end", "correction", "repeat_lookup", "rule", "other")

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

    def add_memory_candidate(self, kind: str, summary: str, *,
                             conversation_id: str | None = None,
                             detail: str | None = None,
                             source: str | None = None) -> dict:
        """Append a memory-worthy candidate (research-lane D5). Append-only; never
        updated or trusted — just collected for Task 3's future write policy."""
        if kind not in MEMORY_CANDIDATE_KINDS:
            kind = "other"
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memory_candidates (ts, conversation_id, kind, summary, "
                "detail, source) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, conversation_id, kind, summary, detail, source),
            )
            self._conn.commit()
            cid = cur.lastrowid
        return {"id": cid, "ts": ts, "conversation_id": conversation_id,
                "kind": kind, "summary": summary, "detail": detail, "source": source}

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
