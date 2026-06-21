import os
import json
import uuid
import asyncio
import pathlib
import ipaddress
from typing import Set, Dict, Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from argus.main import build_registry
from argus import loop
from argus.storage import Store
import httpx
import relogin

STATIC = pathlib.Path(__file__).parent / "static"
PRESETS = pathlib.Path(__file__).parent / "presets"

# Load environment variables
MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:8099/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_DEFAULT_MODEL", "qwen3-next-80b")
HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Build registry once at startup
registry = build_registry()

# Conversation store (the DA seam) — gives the model memory + backs the history UI.
DB_PATH = os.environ.get("ARGUS_DB", "argus.db")
HISTORY_TURNS = int(os.environ.get("ARGUS_HISTORY_TURNS", "20"))  # max prior msgs fed to model
store = Store(DB_PATH)


def _cid(value) -> str:
    """Map a missing/empty conversation_id to the single 'default' thread."""
    return value or "default"


# Background task runner: long jobs run detached + push a phone notification on done.
# Notify via Home Assistant DIRECTLY (Argus already has HA creds) — no dependency on
# the separate iMessage UI app on :8095, which is just a wrapper around this service.
from argus import tasks
NOTIFY_SERVICE = os.environ.get("ARGUS_NOTIFY_SERVICE", "notify/mobile_app_shanes_iphone")


def _notify(title, message):
    if not HA_URL or not HA_TOKEN:
        return
    try:
        httpx.post(f"{HA_URL}/api/services/{NOTIFY_SERVICE}",
                   headers={"Authorization": f"Bearer {HA_TOKEN}"},
                   json={"title": title, "message": message}, timeout=10)
    except Exception:
        pass


task_mgr = tasks.configure(registry=registry, model_name=DEFAULT_MODEL,
                           base_url=MODEL_URL, store=store, notifier=_notify)

# Global state
subscribers: Set[asyncio.Queue] = set()
TASKS: Dict[str, asyncio.Task] = {}

app = FastAPI()


@app.on_event("startup")
async def _bind_task_loop():
    # Capture the server's event loop so background tasks can be scheduled onto it
    # from sync tool calls (run_coroutine_threadsafe).
    task_mgr.loop = asyncio.get_running_loop()
    # Kill any login procs orphaned by a previous server life (see relogin.py).
    relogin.manager.sweep_orphans()


@app.on_event("shutdown")
async def _relogin_shutdown():
    relogin.manager.shutdown()

def publish(event_name: str, data: Any):
    """Publish event to all subscribers."""
    event = (event_name, data)
    for queue in list(subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            subscribers.discard(queue)

# Claude Code as a selectable model — routes to a PERSISTENT per-conversation `claude`
# session (argus/claude_code.py). Bypasses the harness + llama-swap, so NO GPU model
# is loaded/unloaded; each Forge thread keeps native CC context across turns.


async def sse_generator():
    """SSE generator for /argus/events."""
    queue = asyncio.Queue()
    subscribers.add(queue)
    try:
        # unnamed message so the frontend's onmessage fires -> status light goes "up"
        yield 'data: {"hello": true}\n\n'
        while True:
            try:
                event_name, data = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        subscribers.discard(queue)

@app.post("/argus/chat")
async def chat(request: Request):
    body = await request.json()
    model_name = body.get("model", DEFAULT_MODEL)
    message = body.get("message", "")
    conversation_id = _cid(body.get("conversation_id"))
    bubble_id = uuid.uuid4().hex

    # Load prior turns (memory) BEFORE persisting this one, then record the user msg.
    history = await asyncio.to_thread(store.model_history, conversation_id, HISTORY_TURNS)
    user_row = await asyncio.to_thread(
        store.add_message, conversation_id, "user", message, None)

    def on_event(phase, detail, step):
        # Live progress for the UI status pill (tool calls, loop caught).
        publish("bubble_status", {"id": bubble_id, "phase": phase,
                                  "detail": detail, "step": step})

    async def run_chat():
        final = ""
        try:
            if model_name == "claude-code":
                # Persistent per-conversation CC session keeps its own context, so we
                # send only the new message (not the full history).
                from argus import claude_code
                source = claude_code.send(conversation_id, message)
            else:
                source = loop.stream_run(
                    registry, message, model_name=model_name, base_url=MODEL_URL,
                    turn_budget=8, message_history=history, on_event=on_event)
            async for content in source:
                if isinstance(content, tuple) and content[0] == "__event__":
                    ev = content[1]
                    # Keep the always-on status pill driven on tool use (as before)...
                    if ev.get("kind") == "tool_use":
                        on_event("tool", ev.get("name", "tool"), 0)
                    # ...and forward the rich activity to the tap-to-expand panel
                    # (claude-code path only; the 80B path never yields __event__).
                    publish("bubble_activity", {"id": bubble_id, "event": ev})
                    continue
                final = content
                publish("bubble_update", {"id": bubble_id, "content": content})
            row = await asyncio.to_thread(
                store.add_message, conversation_id, "assistant", final, model_name)
            publish("bubble_done", {"id": bubble_id, "db_id": row["id"],
                                    "user_id": user_row["id"], "conversation_id": conversation_id})
        except asyncio.CancelledError:
            if final:  # persist whatever streamed before cancel so memory stays consistent
                await asyncio.to_thread(
                    store.add_message, conversation_id, "assistant", final, model_name)
            publish("bubble_done", {"id": bubble_id, "cancelled": True})
        except Exception as e:
            publish("bubble_done", {"id": bubble_id, "error": str(e)})

    task = asyncio.create_task(run_chat())
    TASKS[bubble_id] = task

    return JSONResponse({"id": bubble_id})

@app.post("/api/cancel/{bubble_id}")
async def cancel(bubble_id: str):
    task = TASKS.get(bubble_id)
    if task:
        task.cancel()
    return JSONResponse({"ok": True})

@app.get("/argus/models")
async def get_models():
    # Forge expects [[id, cfg], ...] where cfg has display/backend.
    return JSONResponse([
        [DEFAULT_MODEL, {"display": "Argus (local 80B)", "backend": "argus"}],
        ["claude-code", {"display": "Claude Code", "backend": "claude"}],
    ])

@app.get("/argus/history")
async def get_history(conversation_id: str | None = None, limit: int = 100,
                     before_id: int | None = None):
    msgs = await asyncio.to_thread(
        store.get_messages, _cid(conversation_id), limit, before_id)
    return JSONResponse(msgs)

@app.get("/argus/conversations")
async def get_conversations():
    convs = await asyncio.to_thread(store.list_conversations)
    return JSONResponse(convs)

@app.get("/argus/audiobook/search")
async def ab_search(q: str):
    from argus.tools import audiobook
    try:
        return JSONResponse(await asyncio.to_thread(audiobook.search, q))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/argus/audiobook/latest")
async def ab_latest(limit: int = 24):
    from argus.tools import audiobook
    try:
        return JSONResponse(await asyncio.to_thread(audiobook.latest, limit))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/argus/audiobook/genres")
async def ab_genres():
    from argus.tools import audiobook
    return JSONResponse(audiobook.genres())

@app.post("/argus/audiobook/grab")
async def ab_grab(request: Request):
    from argus.tools import audiobook
    body = await request.json()
    url = body.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="missing url")
    try:
        return JSONResponse(await asyncio.to_thread(audiobook.grab, url, body.get("title", "")))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/argus/weather/current")
async def weather_current(location: str = "Dahlonega, GA"):
    from argus.tools import weather
    try:
        return JSONResponse(await asyncio.to_thread(weather.current, location))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/argus/weather/forecast")
async def weather_forecast(location: str = "Dahlonega, GA", days: int = 5):
    from argus.tools import weather
    try:
        return JSONResponse(await asyncio.to_thread(weather.forecast, location, days))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/argus/tasks")
async def list_tasks():
    return JSONResponse(task_mgr.list())

@app.get("/argus/tasks/{tid}")
async def get_task(tid: str):
    t = task_mgr.get(tid)
    if not t:
        raise HTTPException(status_code=404)
    return JSONResponse(t)

@app.delete("/argus/history/{message_id}")
async def delete_message(message_id: int):
    await asyncio.to_thread(store.delete_message, message_id)
    return JSONResponse({"ok": True})

@app.delete("/argus/history")
async def delete_history(conversation_id: str | None = None):
    # No conversation_id -> wipe everything; otherwise just that thread.
    await asyncio.to_thread(store.clear, conversation_id)
    return JSONResponse({"ok": True})

@app.delete("/argus/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    await asyncio.to_thread(store.clear, conversation_id)
    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC / "manifest.json", headers={"Cache-Control": "no-cache"})

@app.get("/sw.js")
async def sw_js():
    return FileResponse(STATIC / "sw.js", headers={"Cache-Control": "no-cache"})

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

@app.get("/argus/events")
async def events():
    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@app.get("/layout")
async def get_layout():
    layout_path = PRESETS / "current.json"
    if layout_path.exists():
        return JSONResponse(json.loads(layout_path.read_text()))
    return JSONResponse({})

@app.post("/layout")
async def set_layout(request: Request):
    body = await request.json()
    layout_path = PRESETS / "current.json"
    layout_path.parent.mkdir(exist_ok=True)
    layout_path.write_text(json.dumps(body, indent=2))
    return JSONResponse({"ok": True})

@app.get("/presets/{name}")
async def get_preset(name: str):
    preset_path = PRESETS / f"{name}.json"
    if preset_path.exists():
        return JSONResponse(json.loads(preset_path.read_text()))
    raise HTTPException(status_code=404)

@app.post("/presets/{name}")
async def set_preset(name: str, request: Request):
    body = await request.json()
    preset_path = PRESETS / f"{name}.json"
    preset_path.parent.mkdir(exist_ok=True)
    preset_path.write_text(json.dumps(body, indent=2))
    return JSONResponse({"ok": True})

@app.delete("/presets/{name}")
async def delete_preset(name: str):
    preset_path = PRESETS / f"{name}.json"
    if preset_path.exists():
        preset_path.unlink()
    return JSONResponse({"ok": True})

@app.get("/ha/{path:path}")
@app.post("/ha/{path:path}")
@app.delete("/ha/{path:path}")
async def ha_proxy(path: str, request: Request):
    if not HA_URL or not HA_TOKEN:
        raise HTTPException(status_code=500, detail="HA proxy not configured")
    
    url = f"{HA_URL}/api/{path}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    data = await request.body() if request.method in ["POST", "PUT"] else None
    
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=data,
                timeout=30.0
            )
            return Response(
                content=resp.content,
                media_type=resp.headers.get("content-type", "application/json"),
                status_code=resp.status_code
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")

# ── Remote Claude re-login ────────────────────────────────────────────────────
# Complete a forced `claude auth login` from a phone (see ui/relogin.py). The app
# binds 0.0.0.0, so these routes — which can rewrite credentials — are gated to
# loopback + Tailscale (100.64.0.0/10) only, never the public/LAN interface.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _relogin_guard(request: Request):
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(status_code=403, detail="forbidden")
    if not (ip.is_loopback or ip in _TAILSCALE_NET):
        raise HTTPException(status_code=403,
                            detail="re-login is allowed only from loopback or Tailscale")


@app.get("/relogin/status")
async def relogin_status(request: Request):
    _relogin_guard(request)
    return JSONResponse(await asyncio.to_thread(relogin.manager.auth_status))


@app.post("/relogin/start")
async def relogin_start(request: Request):
    _relogin_guard(request)
    try:
        return JSONResponse(await asyncio.to_thread(relogin.manager.start))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/relogin/code")
async def relogin_code(request: Request):
    _relogin_guard(request)
    body = await request.json()
    sid = body.get("session_id", "")
    code = body.get("code", "")
    if not sid or not code:
        raise HTTPException(status_code=400, detail="missing session_id or code")
    try:
        return JSONResponse(await asyncio.to_thread(relogin.manager.submit_code, sid, code))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARGUS_PORT", "8200")))
