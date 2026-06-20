"""Background task runner — run a long agent job detached, notify on completion.

Argus turns are synchronous/one-shot. This lets the agent DELEGATE a genuinely
long or multi-step job: it runs in the background on the server's event loop,
persists the result, and pushes a phone notification when done. The chat turn that
started it returns immediately ("started, task <id>").

Configured once by the server (configure(...)). The start_background_task tool
calls submit(). Tasks live in memory + their results are persisted as messages;
in-flight tasks do NOT survive a server restart (acceptable v1).
"""
from __future__ import annotations

import asyncio
import time
import uuid


class TaskManager:
    def __init__(self, registry, model_name, base_url, *, store=None,
                 notifier=None, conversation_id="background-tasks", turn_budget=12):
        self.registry = registry
        self.model_name = model_name
        self.base_url = base_url
        self.store = store
        self.notifier = notifier            # notifier(title, message)
        self.cid = conversation_id
        self.turn_budget = turn_budget
        self.loop: asyncio.AbstractEventLoop | None = None   # set at server startup
        self.tasks: dict[str, dict] = {}

    def submit(self, prompt: str) -> str:
        """Schedule a task on the server loop. Safe to call from a tool running in a
        worker thread (uses run_coroutine_threadsafe against the captured loop)."""
        if self.loop is None:
            raise RuntimeError("task manager not bound to an event loop")
        tid = uuid.uuid4().hex[:8]
        self.tasks[tid] = {"id": tid, "prompt": prompt, "status": "running",
                           "result": None, "started": time.time(), "finished": None}
        asyncio.run_coroutine_threadsafe(self._run(tid, prompt), self.loop)
        return tid

    async def _run(self, tid: str, prompt: str) -> None:
        from . import loop as agent_loop
        rec = self.tasks[tid]
        try:
            out = await agent_loop.run_async(
                self.registry, prompt, model_name=self.model_name,
                base_url=self.base_url, turn_budget=self.turn_budget)
            rec["status"], rec["result"] = "done", out
        except Exception as e:
            rec["status"], rec["result"] = "failed", f"{type(e).__name__}: {e}"
        rec["finished"] = time.time()

        if self.store:
            try:
                self.store.add_message(
                    self.cid, "assistant",
                    f"[background task {tid} — {rec['status']}]\n"
                    f"Task: {prompt}\n\n{rec['result']}", self.model_name)
            except Exception:
                pass
        if self.notifier:
            try:
                self.notifier(f"Argus task {rec['status']}",
                              (rec["result"] or "")[:160] or prompt[:160])
            except Exception:
                pass


    def list(self) -> list[dict]:
        return sorted(self.tasks.values(), key=lambda t: t["started"], reverse=True)

    def get(self, tid: str) -> dict | None:
        return self.tasks.get(tid)


_MANAGER: TaskManager | None = None


def configure(**kwargs) -> TaskManager:
    global _MANAGER
    _MANAGER = TaskManager(**kwargs)
    return _MANAGER


def manager() -> TaskManager | None:
    return _MANAGER
