import os
import json
import uuid
import asyncio
import pathlib
from typing import Set, Dict, Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from argus.main import build_registry
from argus import loop
import httpx

STATIC = pathlib.Path(__file__).parent / "static"
PRESETS = pathlib.Path(__file__).parent / "presets"

# Load environment variables
MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:8099/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_DEFAULT_MODEL", "qwen3-next-80b")
HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Build registry once at startup
registry = build_registry()

# Global state
subscribers: Set[asyncio.Queue] = set()
TASKS: Dict[str, asyncio.Task] = {}

app = FastAPI()

def publish(event_name: str, data: Any):
    """Publish event to all subscribers."""
    event = (event_name, data)
    for queue in list(subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            subscribers.discard(queue)

async def sse_generator():
    """SSE generator for /brain/events."""
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

@app.post("/brain/chat")
async def chat(request: Request):
    body = await request.json()
    model_name = body.get("model", DEFAULT_MODEL)
    message = body.get("message", "")
    bubble_id = uuid.uuid4().hex

    async def run_chat():
        try:
            async for content in loop.stream_run(
                registry, message, model_name=model_name, base_url=MODEL_URL, turn_budget=8
            ):
                publish("bubble_update", {"id": bubble_id, "content": content})
            publish("bubble_done", {"id": bubble_id, "db_id": None, "user_id": None})
        except asyncio.CancelledError:
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

@app.get("/brain/models")
async def get_models():
    # Forge expects [[id, cfg], ...] where cfg has display/backend.
    return JSONResponse([[DEFAULT_MODEL, {"display": "Argus (local 80B)", "backend": "argus"}]])

@app.get("/brain/history")
@app.get("/brain/conversations")
async def get_history():
    return JSONResponse([])

@app.delete("/brain/history")
@app.delete("/brain/conversations")
async def delete_history():
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

@app.get("/brain/events")
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARGUS_PORT", "8200")))
