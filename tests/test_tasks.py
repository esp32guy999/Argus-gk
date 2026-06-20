"""Contract test for the background task runner (argus/tasks.py). Offline:
stubs loop.run_async, drives a real event loop. python tests/test_tasks.py
"""
from __future__ import annotations
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import tasks, loop as agent_loop

    async def fake_run_async(registry, prompt, **kw):
        return f"completed: {prompt}"
    agent_loop.run_async = fake_run_async  # _run does `from . import loop` -> same module

    notified, stored = {}, {}

    class FakeStore:
        def add_message(self, cid, role, content, model):
            stored.update(cid=cid, role=role, content=content)

    def notifier(title, message):
        notified.update(title=title, message=message)

    mgr = tasks.TaskManager(registry=None, model_name="m", base_url="u",
                            store=FakeStore(), notifier=notifier)

    async def go():
        mgr.loop = asyncio.get_running_loop()
        tid = mgr.submit("research the thing")
        assert mgr.get(tid)["status"] == "running", "submit returns immediately, running"
        for _ in range(60):
            if mgr.get(tid)["status"] != "running":
                break
            await asyncio.sleep(0.05)
        return tid

    tid = asyncio.run(go())
    rec = mgr.get(tid)

    assert rec["status"] == "done", rec
    assert "research the thing" in rec["result"], rec
    print("PASS: submit runs detached -> status done with result")

    assert notified.get("title", "").endswith("done"), notified
    assert "research the thing" in notified.get("message", ""), notified
    print("PASS: notifier fired on completion")

    assert stored.get("cid") == "background-tasks" and "research the thing" in stored.get("content", ""), stored
    print("PASS: result persisted as a message")

    assert mgr.list()[0]["id"] == tid and mgr.get("nope") is None
    print("PASS: list()/get()")

    # failure path: run_async raises -> status failed, still notifies
    async def boom(*a, **k):
        raise RuntimeError("kaboom")
    agent_loop.run_async = boom

    async def go2():
        mgr.loop = asyncio.get_running_loop()
        tid2 = mgr.submit("doomed")
        for _ in range(60):
            if mgr.get(tid2)["status"] != "running":
                break
            await asyncio.sleep(0.05)
        return tid2
    rec2 = mgr.get(asyncio.run(go2()))
    assert rec2["status"] == "failed" and "kaboom" in rec2["result"], rec2
    print("PASS: failure -> status failed, captured")

    print("\nALL TASKS CONTRACT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
