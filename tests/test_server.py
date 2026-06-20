"""Eval for the Argus web server (ui/server.py) — the gate for the 80B's draft.

Boots the server as a subprocess (pointed at the live 80B), then drives the real
Forge contract: GET /brain/models, POST /brain/chat, and the /brain/events SSE
stream — asserting bubble_update (with content) + bubble_done arrive for the bubble.
Runnable standalone:  python tests/test_server.py   (exit 0 = pass)
"""
from __future__ import annotations
import asyncio, json, os, signal, subprocess, sys, time
import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8271
BASE = f"http://127.0.0.1:{PORT}"
DEFAULT_MODEL = "qwen3-next-80b"


def boot():
    env = {**os.environ, "ARGUS_PORT": str(PORT),
           "PYTHONPATH": ROOT,   # so `import argus` resolves when running ui/server.py
           "ARGUS_MODEL_URL": "http://localhost:8099/v1",
           "ARGUS_DEFAULT_MODEL": DEFAULT_MODEL}
    p = subprocess.Popen([sys.executable, "ui/server.py"], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/", timeout=2).status_code < 500:
                return p
        except Exception:
            pass
        if p.poll() is not None:
            print("SERVER DIED:\n", p.stdout.read().decode()[-1500:]); sys.exit(1)
        time.sleep(1)
    p.terminate(); print("server never came up"); sys.exit(1)


async def chat_roundtrip() -> int:
    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.get(f"{BASE}/brain/models")
        m = r.json()
        # Forge contract: [[id, cfg], ...] with cfg.display — destructured as list.map(([id,cfg])=>...)
        assert r.status_code == 200 and m and isinstance(m[0], list) and len(m[0]) == 2 \
            and isinstance(m[0][1], dict) and "display" in m[0][1], f"bad models shape: {m}"
        print("PASS: GET /brain/models (correct [[id,cfg],...] shape)")

        events = []
        async def reader():
            async with c.stream("GET", f"{BASE}/brain/events") as resp:
                name = data = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:"): name = line[6:].strip()
                    elif line.startswith("data:"): data = line[5:].strip()
                    elif line == "" and name and data is not None:
                        try: events.append((name, json.loads(data)))
                        except Exception: pass
                        name = data = None
        task = asyncio.create_task(reader())
        await asyncio.sleep(1.5)  # ensure subscribed before posting

        resp = await c.post(f"{BASE}/brain/chat",
                            json={"model": DEFAULT_MODEL, "message": "Reply with exactly: pong"})
        assert resp.status_code == 200, resp.text
        bid = resp.json()["id"]
        print(f"PASS: POST /brain/chat -> id={bid}")

        for _ in range(120):
            if any(n == "bubble_done" and d.get("id") == bid for n, d in events):
                break
            await asyncio.sleep(1)
        task.cancel()

        updates = [d for n, d in events if n == "bubble_update" and d.get("id") == bid and d.get("content")]
        dones   = [d for n, d in events if n == "bubble_done" and d.get("id") == bid]
        assert updates, f"no bubble_update with content for {bid}; events={events[:6]}"
        assert dones, f"no bubble_done for {bid}"
        print(f"PASS: SSE streamed {len(updates)} bubble_update(s) + bubble_done")
        print("   final content:", updates[-1]["content"][:120])
        return 0


def main() -> int:
    # secrets gate
    src = open(os.path.join(ROOT, "ui/server.py")).read()
    import re
    if re.search(r"eyJ[A-Za-z0-9_-]{20,}|[0-9a-f]{32}|Bearer [A-Za-z0-9]{8}", src):
        print("FAIL: hardcoded secret in ui/server.py"); return 1
    print("PASS: no hardcoded secrets")

    p = boot()
    try:
        return asyncio.run(chat_roundtrip())
    finally:
        p.send_signal(signal.SIGTERM)
        try: p.wait(timeout=5)
        except Exception: p.kill()


if __name__ == "__main__":
    print("ALL SERVER EVAL PASSED" if main() == 0 else "SERVER EVAL FAILED")
