import os
import json
import uuid
import asyncio
import pathlib
import ipaddress
import time
from typing import Set, Dict, Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from argus.main import build_registry
from argus import loop, metrics
from argus.storage import Store
from prometheus_client import make_asgi_app
import httpx
import relogin

STATIC = pathlib.Path(__file__).parent / "static"
PRESETS = pathlib.Path(__file__).parent / "presets"

# Load environment variables
MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:8099/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_DEFAULT_MODEL", "qwen3-next-80b")

# Models NOT served by llama-swap — routed to their own OpenAI-compatible endpoint.
# gemma4-cpu = Gemma 4 E2B on a CPU-only ollama (CUDA hidden) → runs on the 7800X3D and
# never touches the 5080's VRAM (which the reasoner owns). Only ollama can load gemma4.
EXTERNAL_MODELS = {
    "gemma4-cpu": {"base_url": "http://localhost:11435/v1", "model_id": "gemma4e2b",
                   "display": "Gemma 4 E2B (CPU)"},
}

# Vision capability is per-model — never forward images to a model that can't see
# them (wastes tokens / errors). Gate on this set, not on backend or model name.
# claude-code (Claude) is vision-capable; local llama-swap models here are not.
VISION_MODELS = {"claude-code"}

def _is_vision(model_name: str) -> bool:
    return model_name in VISION_MODELS

# Models slow to cold-load (the local 80B offloads MoE layers to CPU → ~1-2 min cold).
# These get warmed on selection and announce readiness (phone buzz + UI pill) so the
# first turn doesn't look like a dead box. Fast/cloud models aren't warmed.
WARM_ON_SELECT = {"qwen3-next-80b"}
_warming: set = set()   # models with an in-flight warm-up (dedupe rapid selects)

def _warm_on_select(model_name: str) -> bool:
    return model_name in WARM_ON_SELECT

# Thinking-capable local models that should run with reasoning OFF on the interactive
# chat path. Qwen3.6 reasons on everything, which makes chat sluggish and (on structured
# turns) can run the token budget dry before answering; the 2026-06-24 bake-off showed the
# reasoning is correct but ~50x slower for the same result. With thinking off it answers
# ~0.5s and STILL calls tools correctly (verified). Maps model -> enable_thinking value
# passed to loop.stream_run; absent -> None (server default, unchanged).
CHAT_THINKING = {"qwen3.6-35b-a3b": False}

# Per-model context window (tokens). The local 80B is served at -c 8192 and is
# VRAM-bound there — history MUST be budgeted to fit or llama.cpp truncates the
# prompt from the front (dropping the system prompt) or errors. Unknown local
# models default conservatively; claude-code manages its own context (and isn't
# even fed this history), so it's exempt.
CONTEXT_WINDOW = {"qwen3-next-80b": 8192}
_DEFAULT_LOCAL_CTX = 8192
# Tokens reserved within the window for the system prompt + selected tool schemas
# + the live user prompt + room for the reply. The remainder is the history budget.
HISTORY_RESERVE = 3500

def _history_budget(model_name: str) -> int | None:
    """Max tokens of prior history to feed this model, or None to skip budgeting
    (claude-code: history isn't sent to it and it self-manages context)."""
    if model_name == "claude-code":
        return None
    window = CONTEXT_WINDOW.get(model_name, _DEFAULT_LOCAL_CTX)
    return max(512, window - HISTORY_RESERVE)
HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Build registry once at startup
registry = build_registry()

# Conversation store (the DA seam) — gives the model memory + backs the history UI.
DB_PATH = os.environ.get("ARGUS_DB", "argus.db")
HISTORY_TURNS = int(os.environ.get("ARGUS_HISTORY_TURNS", "20"))  # max prior msgs fed to model
store = Store(DB_PATH)


# Local-model serving goes through llama-swap (MODEL_URL → :9090), which loads the
# requested model on demand and keeps one resident at a time. No launcher lives here:
# the 80B is just another llama-swap entry now. claude-code stays on its own cloud
# path (see chat()), so selecting it never touches llama-swap or evicts the 80B.
_LOCAL_BASE = MODEL_URL.rsplit("/v1", 1)[0]


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

# Pollable per-conversation turn status. A long, tool-heavy claude-code turn pushes its
# progress over publish()->SSE, which iOS silently drops on backgrounding — so the UI goes
# quiet and "thinking" becomes indistinguishable from "stalled". This dict is the robust
# fallback: the client POLLS it, so a ticking timer + current activity are always visible.
TURN_STATUS: Dict[str, dict] = {}

def _turn_begin(cid, bubble_id):
    TURN_STATUS[cid] = {"active": True, "bubble_id": bubble_id,
                        "started": time.time(), "last_activity": time.time(),
                        "phase": "starting", "detail": ""}

def _turn_touch(cid, phase=None, detail=None):
    s = TURN_STATUS.get(cid)
    if not s:
        return
    s["last_activity"] = time.time()
    if phase is not None:
        s["phase"] = phase
    if detail is not None:
        s["detail"] = detail

def _turn_end(cid):
    s = TURN_STATUS.get(cid)
    if s:
        s["active"] = False

app = FastAPI()
# Expose harness metrics on the LIVE server (the CLI path called metrics.serve(); the
# server never did, so until now everything was scraped by nobody). Scrape :8210/metrics.
app.mount("/metrics", make_asgi_app())


@app.on_event("startup")
async def _bind_task_loop():
    # Capture the server's event loop so background tasks can be scheduled onto it
    # from sync tool calls (run_coroutine_threadsafe).
    task_mgr.loop = asyncio.get_running_loop()
    # Kill any login procs orphaned by a previous server life (see relogin.py).
    relogin.manager.sweep_orphans()
    # Self-observer: watch our own metrics + buzz on trouble (the consumer that makes
    # the Prometheus cornerstone actually function instead of being scraped by nobody).
    from argus import observability
    asyncio.create_task(observability.SelfObserver(_notify).run())
    # The Work Ledger reconciler: advance/notify in-flight jobs without a human poke.
    from argus import ledger
    asyncio.create_task(ledger.reconcile_loop(store, publish))


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
    attachments = body.get("attachments") or []
    # Only forward images to vision-capable models — don't apply attachments generically.
    if attachments and not _is_vision(model_name):
        attachments = []
    conversation_id = _cid(body.get("conversation_id"))
    bubble_id = uuid.uuid4().hex
    _turn_begin(conversation_id, bubble_id)

    # Load prior turns (memory) BEFORE persisting this one, then record the user msg.
    history = await asyncio.to_thread(store.model_history, conversation_id, HISTORY_TURNS,
                                      _history_budget(model_name))
    user_row = await asyncio.to_thread(
        store.add_message, conversation_id, "user", message, None)

    def on_event(phase, detail, step):
        # Live progress for the UI status pill (tool calls, loop caught).
        _turn_touch(conversation_id, phase, detail)
        publish("bubble_status", {"id": bubble_id, "phase": phase,
                                  "detail": detail, "step": step})

    async def run_chat():
        final = ""
        try:
            if model_name == "claude-code":
                # Persistent per-conversation CC session keeps its own context, so we
                # send only the new message (not the full history).
                from argus import claude_code
                source = claude_code.send(conversation_id, message, attachments=attachments)
            else:
                # Local models: not in VISION_MODELS, so attachments were already
                # dropped above. (Wiring a vision-capable local model would mean adding
                # it to VISION_MODELS and threading images into loop.stream_run.)
                # External models (gemma4-cpu) -> their own endpoint + real model id;
                # everything else -> llama-swap.
                ext = EXTERNAL_MODELS.get(model_name)
                source = loop.stream_run(
                    registry, message,
                    model_name=(ext["model_id"] if ext else model_name),
                    base_url=(ext["base_url"] if ext else MODEL_URL),
                    turn_budget=8, message_history=history, on_event=on_event,
                    enable_thinking=CHAT_THINKING.get(model_name))
            async for content in source:
                if isinstance(content, tuple) and content[0] == "__event__":
                    ev = content[1]
                    # Keep the always-on status pill driven on tool use (as before)...
                    if ev.get("kind") == "tool_use":
                        on_event("tool", ev.get("name", "tool"), 0)
                    elif ev.get("kind") == "done":   # CC metric: turn wall-clock time
                        if ev.get("duration_ms"):
                            metrics.CC_DURATION.observe(ev["duration_ms"] / 1000)
                    # ...and forward the rich activity to the tap-to-expand panel
                    # (claude-code path only; the 80B path never yields __event__).
                    publish("bubble_activity", {"id": bubble_id, "event": ev})
                    continue
                final = content
                _turn_touch(conversation_id, "writing", "")
                publish("bubble_update", {"id": bubble_id, "content": content})
            if model_name == "claude-code":
                metrics.CC_TURNS.labels("ok").inc()
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
            if model_name == "claude-code":
                metrics.CC_TURNS.labels("error").inc()
            publish("bubble_done", {"id": bubble_id, "error": str(e)})
        finally:
            _turn_end(conversation_id)

    task = asyncio.create_task(run_chat())
    TASKS[bubble_id] = task

    return JSONResponse({"id": bubble_id})


@app.get("/argus/turn_status")
async def turn_status(conversation_id: str | None = None):
    """Pollable 'is a turn running + what's it doing' — the robust (non-SSE) heartbeat the
    UI polls to show a ticking 'working…' pill, so thinking is visibly != stalled."""
    s = TURN_STATUS.get(_cid(conversation_id))
    if not s or not s.get("active"):
        return JSONResponse({"active": False})
    now = time.time()
    return JSONResponse({
        "active": True,
        "elapsed": round(now - s["started"]),
        "since_activity": round(now - s["last_activity"]),
        "phase": s.get("phase", ""),
        "detail": s.get("detail", ""),
    })

@app.post("/api/cancel/{bubble_id}")
async def cancel(bubble_id: str):
    task = TASKS.get(bubble_id)
    if task:
        task.cancel()
    return JSONResponse({"ok": True})

@app.get("/argus/models")
async def get_models():
    # Forge expects [[id, cfg], ...] where cfg has display/backend.
    # Dynamic: list whatever llama-swap currently serves (so the selector swaps
    # among real local models), then always append the cloud claude-code option.
    # claude-code is a separate path — selecting it never hits llama-swap, so it
    # never evicts a resident local model (e.g. the 80B stays loaded).
    entries = []
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            data = (await c.get(f"{_LOCAL_BASE}/v1/models")).json()
        for m in data.get("data", []):
            mid = m["id"]
            display = "Argus (local 80B)" if mid == DEFAULT_MODEL else mid
            entries.append([mid, {"display": display, "backend": "argus",
                                  "vision": _is_vision(mid), "warm_on_select": _warm_on_select(mid)}])
    except Exception:
        # llama-swap unreachable — still offer the default so the UI isn't empty.
        entries.append([DEFAULT_MODEL, {"display": "Argus (local 80B)", "backend": "argus",
                                        "vision": _is_vision(DEFAULT_MODEL), "warm_on_select": _warm_on_select(DEFAULT_MODEL)}])
    # external (non-llama-swap) models — e.g. the CPU Gemma running on its own ollama
    for name, cfg in EXTERNAL_MODELS.items():
        entries.append([name, {"display": cfg["display"], "backend": "argus",
                               "vision": False, "warm_on_select": False}])
    entries.append(["claude-code", {"display": "Claude Code", "backend": "claude",
                                    "vision": _is_vision("claude-code"), "warm_on_select": False}])
    return JSONResponse(entries)


async def _model_ready(model_name: str) -> bool:
    """True if llama-swap already has this model resident and ready."""
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            data = (await c.get(f"{_LOCAL_BASE}/running")).json()
        return any(r.get("model") == model_name and r.get("state") == "ready"
                   for r in data.get("running", []))
    except Exception:
        return False


async def _warm_model(model_name: str, display: str):
    """Fire a 1-token completion to force the cold load, then buzz + UI-pill on ready."""
    t0 = time.monotonic()
    ok = False
    try:
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(f"{MODEL_URL}/chat/completions",
                             json={"model": model_name, "max_tokens": 1, "temperature": 0,
                                   "messages": [{"role": "user", "content": "ok"}]})
            ok = r.status_code == 200
    except Exception:
        ok = False
    finally:
        _warming.discard(model_name)
    secs = round(time.monotonic() - t0)
    if ok:
        await asyncio.to_thread(_notify, f"🟢 {display} ready",
                                f"Finished loading in {secs}s — go ahead.")
        publish("model_warm", {"model": model_name, "ready": True, "seconds": secs})
    else:
        await asyncio.to_thread(_notify, f"⚠️ {display} load failed",
                                "The model didn't come up — check llama-swap.")
        publish("model_warm", {"model": model_name, "error": True, "seconds": secs})


@app.post("/argus/warm")
async def warm(request: Request):
    """Warm a slow-to-load model on selection; notify (phone + UI) when ready.
    Idempotent: already-ready or already-warming returns immediately, no extra load."""
    body = await request.json()
    model_name = body.get("model") or ""
    display = body.get("display") or model_name
    if not _warm_on_select(model_name):
        return JSONResponse({"status": "skip"})
    if await _model_ready(model_name):
        return JSONResponse({"status": "ready", "already": True})
    if model_name in _warming:
        return JSONResponse({"status": "warming", "already": True})
    _warming.add(model_name)
    publish("model_warm", {"model": model_name, "loading": True})
    asyncio.create_task(_warm_model(model_name, display))
    return JSONResponse({"status": "warming"})

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
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC / "manifest.json", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/sw.js")
async def sw_js():
    return FileResponse(STATIC / "sw.js", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

# Static assets (app.js, styles.css, widgets) were served with NO cache header, so iOS
# Safari heuristically cached them forever — the reason we kept bumping ?v=NN all week.
# Force every static response to revalidate.
class NoStoreStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

app.mount("/static", NoStoreStatic(directory=str(STATIC)), name="static")

@app.get("/argus/events")
async def events():
    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@app.post("/argus/inject")
async def inject_message(request: Request):
    """Persist a message AND push it live over SSE so open clients append it
    without a reload. Used to post widgets/cards (e.g. ```html embeds) into chat."""
    body = await request.json()
    cid = _cid(body.get("conversation_id"))
    role = body.get("role", "assistant")
    content = body.get("content", "")
    model = body.get("model")
    if not content:
        return JSONResponse({"error": "empty content"}, status_code=400)
    row = await asyncio.to_thread(store.add_message, cid, role, content, model)
    publish("chat_message", {"role": role, "content": content, "model": model,
                             "conversation_id": cid, "id": row.get("id")})
    return JSONResponse({"ok": True, "id": row.get("id")})


# ── The Work Ledger — durable in-flight job board (docs/DESIGN-work-ledger.md) ────
@app.get("/argus/jobs")
async def list_jobs(include_terminal: bool = True):
    """The board. Live work first, then most-recently finished. The reconciler keeps
    rows fresh on its own tick; this just returns the current state."""
    jobs = await asyncio.to_thread(store.list_jobs, include_terminal=include_terminal)
    return JSONResponse({"jobs": jobs})

@app.get("/argus/jobs/{job_id}")
async def get_job(job_id: str):
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return JSONResponse(job)

@app.post("/argus/jobs")
async def create_job(request: Request):
    """Register a job. A 'proposed' job sits on the board awaiting approve/reject
    and is NOT run until approved (plan-preview-before-execute)."""
    b = await request.json()
    if not b.get("id") or not b.get("kind") or not b.get("title"):
        return JSONResponse({"error": "id, kind, title required"}, status_code=400)
    try:
        job = await asyncio.to_thread(
            store.add_job, b["id"], b["kind"], b["title"],
            state=b.get("state", "queued"), category=b.get("category", "external"),
            progress=b.get("progress"), detail=b.get("detail"),
            probe=b.get("probe"), payload=b.get("payload"),
            next_check_ts=b.get("next_check_ts"),
            conversation_id=_cid(b.get("conversation_id")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(job)

@app.post("/argus/jobs/{job_id}/{action}")
async def job_action(job_id: str, action: str):
    """approve (proposed→queued), reject/cancel (→cancelled). Approve hands the job
    to the reconciler; reject/cancel takes it off the live board."""
    target = {"approve": "queued", "reject": "cancelled", "cancel": "cancelled"}.get(action)
    if target is None:
        raise HTTPException(status_code=400, detail="action must be approve|reject|cancel")
    job = await asyncio.to_thread(store.update_job, job_id, state=target)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return JSONResponse(job)


_BOARD_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Argus · Work Ledger</title><style>
:root{--bg:#0e0f13;--card:#1a1c22;--line:#2a2d36;--fg:#e8e8ea;--mut:#9aa0aa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.4 -apple-system,system-ui,sans-serif;padding:14px}
h1{font-size:15px;margin:0 0 12px;color:var(--mut);font-weight:600;letter-spacing:.3px}
.job{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin-bottom:9px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.title{font-weight:600}.detail{color:var(--mut);font-size:12px;margin-top:3px}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:600;white-space:nowrap}
.s-active{background:#13361f;color:#4ade80}.s-blocked{background:#3a2c12;color:#fbbf24}
.s-queued{background:#1e2a3a;color:#60a5fa}.s-proposed{background:#2c1e3a;color:#c084fc}
.s-done{background:#1c2530;color:#7d8590}.s-failed{background:#3a1818;color:#f87171}
.s-cancelled{background:#222;color:#666}
.bar{height:5px;background:#22252d;border-radius:3px;margin-top:8px;overflow:hidden}
.fill{height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa)}
.cat{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin-left:6px}
.acts{margin-top:9px;display:flex;gap:7px}button{font:inherit;font-size:12px;font-weight:600;
border:0;border-radius:7px;padding:5px 12px;cursor:pointer}.ap{background:#16432a;color:#4ade80}
.rj{background:#3a1c1c;color:#f87171}.empty{color:var(--mut);text-align:center;padding:30px}
.idle{display:none;background:linear-gradient(135deg,#1a1c22,#15171c);border:1px dashed var(--line);
border-radius:10px;padding:20px;text-align:center;color:var(--mut);font-weight:600;letter-spacing:.4px;margin-bottom:10px}
.idle.show{display:block}
.foot{color:var(--mut);font-size:11px;margin-top:10px;text-align:center}
</style></head><body><h1>◷ ARGUS · WORK LEDGER</h1><div id=idle class=idle>◷ Idle — awaiting assignment</div><div id=board></div>
<div class=foot id=foot></div><script>
const stamp=t=>t?new Date(t*1000).toLocaleTimeString():'';
const LIVE=['active','blocked','queued','proposed'];
async function act(id,a){await fetch(`/argus/jobs/${id}/${a}`,{method:'POST'});load();}
async function load(){
 let j;try{j=await(await fetch('/argus/jobs')).json();}catch(e){return;}
 const b=document.getElementById('board');const jobs=j.jobs||[];
 const live=jobs.filter(x=>LIVE.includes(x.state));
 document.getElementById('idle').classList.toggle('show', live.length===0);
 b.innerHTML=!jobs.length?'':jobs.map(x=>{
  const p=x.progress!=null?`<div class=bar><div class=fill style="width:${Math.round(x.progress*100)}%"></div></div>`:'';
  const a=x.state==='proposed'?`<div class=acts><button class=ap onclick="act('${x.id}','approve')">Approve</button>`+
    `<button class=rj onclick="act('${x.id}','reject')">Reject</button></div>`:'';
  return `<div class=job><div class=top><span class=title>${x.title}<span class=cat>${x.category}</span></span>`+
   `<span class="badge s-${x.state}">${x.state}</span></div>`+
   `${x.detail?`<div class=detail>${x.detail}</div>`:''}${p}${a}</div>`;
 }).join('');
 document.getElementById('foot').textContent=(live.length?live.length+' live · ':'')+'updated '+new Date().toLocaleTimeString();
}
load();setInterval(load,5000);
</script></body></html>"""

@app.get("/argus/board")
async def board():
    """Self-contained, same-origin Work Ledger board — live job states, progress bars,
    and approve/reject for proposed jobs. Polls /argus/jobs every 5s. Open directly or
    iframe it. Same-origin so no sandbox/CORS friction."""
    return Response(content=_BOARD_HTML, media_type="text/html")

# ── qBittorrent proxy — powers the Downloads canvas widget (/qbt/torrents/info).
# Holds a logged-in session (cookie jar) and re-auths on expiry. Creds from env.
_QBT_URL = os.environ.get("QBT_URL", "http://192.168.4.206:8090")
_qbt = httpx.AsyncClient(base_url=_QBT_URL, timeout=10.0)
_qbt_authed = False

async def _qbt_login() -> bool:
    u, p = os.environ.get("QBIT_USER"), os.environ.get("QBIT_PASS")
    if not (u and p):
        return False
    try:
        r = await _qbt.post("/api/v2/auth/login", data={"username": u, "password": p},
                            headers={"Referer": _QBT_URL})   # qBt CSRF needs Referer
        return r.status_code < 300 and "Ok" in r.text
    except Exception:
        return False

@app.get("/qbt/{path:path}")
async def qbt_proxy(path: str, request: Request):
    global _qbt_authed
    if not _qbt_authed:
        _qbt_authed = await _qbt_login()
    async def _get():
        return await _qbt.get(f"/api/v2/{path}", params=dict(request.query_params))
    try:
        r = await _get()
        if r.status_code == 403:                          # session expired -> re-login
            _qbt_authed = await _qbt_login()
            r = await _get()
    except Exception as e:
        return JSONResponse({"error": f"qbit unreachable: {e}"}, status_code=502)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))

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
