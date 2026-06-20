# Argus Web Server — build spec

Write **`ui/server.py`**: a FastAPI app that serves the forked Forge PWA and backs its
`/brain/*` chat contract with the Argus harness. Replaces Forge's Hermes-wired server.
Create ONLY `ui/server.py`. Target ~200 lines.

## Absolute rules
- **NO hardcoded secrets.** Every secret/host comes from `os.environ`. (HA token, etc.)
- Use the harness, don't reinvent it.

## Harness API (import and use these)
```python
from argus.main import build_registry      # build ONCE at startup -> registry (loads all tool lanes)
from argus import loop                      # loop.stream_run(...) is an async generator
# loop.stream_run(registry, message, *, model_name=..., base_url=..., turn_budget=8)
#   -> async-yields CUMULATIVE assistant text (full-text-so-far each step). Raises on error.
```
Model endpoint from env: `MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:8099/v1")`,
`DEFAULT_MODEL = os.environ.get("ARGUS_DEFAULT_MODEL", "qwen3-next-80b")`.

## SSE event bus (core mechanism)
Keep a set of subscriber `asyncio.Queue`s. A `publish(event_name, data_dict)` puts
`(event_name, data_dict)` on every queue. `GET /brain/events` is an SSE response that
registers a new queue and yields each as:  `f"event: {name}\ndata: {json.dumps(data)}\n\n"`
(remove the queue on disconnect). Send a `: keepalive\n\n` comment every ~15s.

## Routes
- `POST /brain/chat`  body `{model, message, conversation_id?, attachments?, use_hermes?}`:
  make `bubble_id = uuid4().hex`; create an asyncio task (store in `TASKS[bubble_id]`) that:
  `async for content in loop.stream_run(registry, message, model_name=<body model or DEFAULT_MODEL>, base_url=MODEL_URL): publish("bubble_update", {"id": bubble_id, "content": content})`
  then `publish("bubble_done", {"id": bubble_id, "db_id": None, "user_id": None})`;
  on `asyncio.CancelledError` → `publish("bubble_done", {"id": bubble_id, "cancelled": True})`;
  on other Exception `e` → `publish("bubble_done", {"id": bubble_id, "error": str(e)})`.
  **Return immediately** `{"id": bubble_id}` (don't await the task).
- `POST /api/cancel/{bubble_id}`: `TASKS.get(bubble_id)` → `.cancel()`; return `{"ok": True}`.
- `GET /brain/models`: return `[{"id": DEFAULT_MODEL, "label": "Argus (local 80B)", "backend": "argus"}]`.
- `GET /brain/history`, `GET /brain/conversations`: return `[]` (v1 stub). `DELETE` variants: return `{"ok": True}`.
- **Static:** `GET /` → FileResponse `static/index.html` (no-cache headers). Mount `/static` → the `static/` dir. `GET /manifest.json` and `GET /sw.js` → those files from static.
- **HA proxy** (env `HA_URL`, `HA_TOKEN`): `GET/POST/DELETE /ha/{path:path}` → forward to `f"{HA_URL}/api/{path}"` with `Authorization: Bearer {HA_TOKEN}` (httpx), return the JSON/body + status.
- **Layout/presets:** `GET /layout` → read `presets/current.json` (or `{}`); `POST /layout` → write it. `GET/POST/DELETE /presets/{name}` → read/write/delete `presets/{name}.json`. (Local files, no secrets.)

## Run
`if __name__ == "__main__": import uvicorn; uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARGUS_PORT", "8200")))`
Build the registry at module load (so the eval can import `app`). Use `httpx` for proxying.
Allowed deps: fastapi, uvicorn, httpx, stdlib. `STATIC = pathlib.Path(__file__).parent / "static"`.
