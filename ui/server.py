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
from argus import loop, metrics, model_config
from argus.storage import get_store
from prometheus_client import make_asgi_app
import httpx
import relogin
import nec

STATIC = pathlib.Path(__file__).parent / "static"
PRESETS = pathlib.Path(__file__).parent / "presets"

# Load environment variables
MODEL_URL = os.environ.get("ARGUS_MODEL_URL", "http://localhost:8099/v1")
DEFAULT_MODEL = os.environ.get("ARGUS_DEFAULT_MODEL", "qwen3-next-80b")

# "Selected = default" — the effective default (chat fallback + background tasks)
# follows the user's last-selected LOCAL model, so the hardcoded 80B stops getting
# pulled in behind their back. Guarded by a capability floor: only models that can
# actually drive tool lanes are eligible; a media / CPU-tiny / cloud pick falls back
# to DEFAULT_MODEL so autonomous tasks don't faceplant on an unfit model.
TASK_CAPABLE_MODELS = {
    "qwen3-next-80b", "qwen3-coder-30b", "qwen3-coder-next",
    "gemma4-26b", "gemma4-12b", "qwen3.6-35b-a3b",
}

def _default_model() -> str:
    """Last-selected local model when it's tool-capable, else the env floor."""
    try:
        layout = json.loads((PRESETS / "current.json").read_text())
        sel = layout.get("lastLocalModel") or layout.get("lastModel")
        if sel in TASK_CAPABLE_MODELS:
            return sel
    except Exception:
        pass
    return DEFAULT_MODEL

# Per-model config (display / vision / warm / thinking / context / external backend)
# now lives in ONE manifest: config/models.yaml, read via argus.model_config. A model
# absent from it resolves to safe defaults, so it's usable on first load. The helpers
# below are thin adapters over the manifest so the rest of the server is unchanged.
_warming: set = set()   # models with an in-flight warm-up (dedupe rapid selects)

def _is_vision(model_name: str) -> bool:
    # Never forward images to a model that can't see them (wastes tokens / errors).
    return model_config.is_vision(model_name)

def _warm_on_select(model_name: str) -> bool:
    # Slow-cold-loading models (e.g. the 80B) warm on select + announce readiness.
    return model_config.warm_on_select(model_name)

def _thinking_for_turn(model_name: str, message: str):
    """Per-TURN thinking policy from the manifest (null → server default). Chat runs
    reasoning OFF for models that otherwise burn the budget dry (qwen3.6, Loki) — off
    is ~50x faster for the same result and tools still fire; the soul rules carry the
    act-don't-narrate burden. Revisit once llama.cpp gains --reasoning-budget."""
    return model_config.thinking(model_name)

# Tokens reserved within the window for the system prompt + selected tool schemas
# + the live user prompt + room for the reply. The remainder is the history budget.
HISTORY_RESERVE = 3500

def _history_budget(model_name: str) -> int | None:
    """Max tokens of prior history to feed this model, or None to skip budgeting
    (claude-code / grok: history isn't sent — the OAuth CLI self-manages context).
    The 80B is VRAM-bound at -c 8192, so history MUST fit or llama.cpp truncates
    from the front."""
    if model_name in ("claude-code", "grok"):
        return None
    return max(512, model_config.context_window(model_name) - HISTORY_RESERVE)
HA_URL = os.environ.get("HA_URL")
HA_TOKEN = os.environ.get("HA_TOKEN")

# Build registry once at startup
registry = build_registry()

# Conversation store (the DA seam) — gives the model memory + backs the history UI.
DB_PATH = os.environ.get("ARGUS_DB", "argus.db")
HISTORY_TURNS = int(os.environ.get("ARGUS_HISTORY_TURNS", "20"))  # max prior msgs fed to model
store = get_store(DB_PATH)   # canonical process-wide store; tools reach the same DB


# Local-model serving goes through llama-swap (MODEL_URL → :9090), which loads the
# requested model on demand and keeps one resident at a time. No launcher lives here:
# the 80B is just another llama-swap entry now. `grok` (SuperGrok OAuth) stays on its
# own cloud path (see chat()), so selecting it never touches llama-swap or evicts the 80B.
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
                           model_resolver=_default_model,
                           base_url=MODEL_URL, store=store, notifier=_notify)

# Global state
subscribers: Set[asyncio.Queue] = set()
TASKS: Dict[str, asyncio.Task] = {}

# Pollable per-conversation turn status. A long, tool-heavy claude-code turn pushes its
# progress over publish()->SSE, which iOS silently drops on backgrounding — so the UI goes
# quiet and "thinking" becomes indistinguishable from "stalled". This dict is the robust
# fallback: the client POLLS it, so a ticking timer + current activity are always visible.
TURN_STATUS: Dict[str, dict] = {}

def _turn_begin(cid, bubble_id, model_name: str | None = None, base_url: str | None = None):
    TURN_STATUS[cid] = {"active": True, "bubble_id": bubble_id,
                        "started": time.time(), "last_activity": time.time(),
                        "phase": "starting", "detail": ""}
    # SS generation pulse — every seated model, including Grok / CC / externals.
    try:
        from argus.ss.sensors import begin_turn
        url = base_url
        if not url and model_name:
            ext = model_config.external(model_name)
            url = (ext or {}).get("base_url") if ext else MODEL_URL
        begin_turn(model_name or "", base_url=url or MODEL_URL)
    except Exception:
        pass

def _turn_touch(cid, phase=None, detail=None):
    s = TURN_STATUS.get(cid)
    if not s:
        return
    s["last_activity"] = time.time()
    if phase is not None:
        s["phase"] = phase
    if detail is not None:
        s["detail"] = detail
    try:
        from argus.ss.sensors import mark_activity
        mark_activity()
    except Exception:
        pass

def _turn_end(cid):
    s = TURN_STATUS.get(cid)
    if s:
        s["active"] = False
    try:
        from argus.ss.sensors import end_turn
        end_turn()
    except Exception:
        pass

app = FastAPI()
# Expose harness metrics on the LIVE server (the CLI path called metrics.serve(); the
# server never did, so until now everything was scraped by nobody). Scrape :8210/metrics.
app.mount("/metrics", make_asgi_app())


async def _model_load_watcher(interval: float = 5.0):
    """Poll llama-swap's /running and phone-push whenever the resident model CHANGES —
    i.e. a new model was loaded into Forge (chat swap, warm, whatever). Skips the boot
    baseline (no buzz for what's already loaded when the server starts); an idle-evict
    resets state so reloading even the same model counts as a fresh load."""
    last, primed = None, False
    while True:
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                data = (await c.get(f"{_LOCAL_BASE}/running")).json()
            ready = [r.get("model") for r in data.get("running", []) if r.get("state") == "ready"]
            cur = ready[0] if ready else None
            if not primed:
                last, primed = cur, True                 # boot baseline — don't buzz
            elif cur and cur != last:
                display = model_config.display(cur) or cur
                await asyncio.to_thread(_notify, "🧠 Model loaded in Forge",
                                        f"{display} is now resident on the GPU.")
                publish("model_loaded", {"model": cur, "display": display})
                last = cur
            elif not cur:
                last = None                              # evicted → next load (even same) buzzes
        except Exception:
            pass
        await asyncio.sleep(interval)


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
    # Buzz the phone whenever llama-swap loads a NEW model into Forge (any swap).
    asyncio.create_task(_model_load_watcher())


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

# OAuth agent paths (Grok via grok_code; legacy claude-code still routable):
# persistent per-conversation CLI sessions. Bypasses harness + llama-swap — no GPU
# load/unload; each Forge thread keeps native agent context across turns.


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
    model_name = body.get("model") or _default_model()
    message = body.get("message", "")
    raw_attachments = body.get("attachments") or []
    conversation_id = _cid(body.get("conversation_id"))
    bubble_id = uuid.uuid4().hex

    # Materialise every attachment to disk — never silently drop. Hard failures
    # (size / decode) return 400 so the UI can show them; soft paths always leave
    # a path + OCR/text extract for non-vision models (argus.attachments).
    from argus import attachments as attmod
    try:
        mats = await asyncio.to_thread(
            attmod.materialize, raw_attachments, conversation_id) if raw_attachments else []
    except attmod.AttachmentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"attachment failed: {e}")

    store_text = attmod.user_message_for_store(message, mats)
    # Model-facing prompt: native vision agents get raw message + Attachment objs;
    # everyone else gets OCR / inline text / path enrichment (no black hole).
    if mats and not attmod.supports_native_vision(model_name):
        model_message = await asyncio.to_thread(attmod.enrich_prompt, message, mats)
    else:
        model_message = message

    _turn_begin(conversation_id, bubble_id, model_name=model_name)

    # Load prior turns (memory) BEFORE persisting this one, then record the user msg.
    history = await asyncio.to_thread(store.model_history, conversation_id, HISTORY_TURNS,
                                      _history_budget(model_name))
    user_row = await asyncio.to_thread(
        store.add_message, conversation_id, "user", store_text, None)

    def on_event(phase, detail, step):
        # Live progress for the UI status pill (tool calls, loop caught).
        _turn_touch(conversation_id, phase, detail)
        publish("bubble_status", {"id": bubble_id, "phase": phase,
                                  "detail": detail, "step": step})

    async def run_chat():
        final = ""
        tools_for_policy: list[str] = []
        # Loop sink phases: silent (NO_REPLY) / blocked (empty after retry).
        turn_flags = {"silent": False, "blocked": False}
        try:
            if model_name == "claude-code":
                # Persistent per-conversation CC session keeps its own context, so we
                # send only the new message (not the full history).
                from argus import claude_code
                cc_atts = attmod.to_claude_ui_attachments(mats) if mats else None
                # Non-image files: append path markers to text for CC tools.
                cc_text = model_message
                if mats:
                    non_img = [a for a in mats if not a.is_image]
                    if non_img:
                        extra = "\n".join(
                            f"[Attached file on disk: {a.path} ({a.filename})]"
                            for a in non_img)
                        cc_text = (cc_text + "\n" + extra).strip() if cc_text else extra
                source = claude_code.send(conversation_id, cc_text, attachments=cc_atts)
            elif model_name == "grok":
                # SuperGrok OAuth via Grok Build CLI — multimodal via --prompt-json.
                # S4: inject SEE worker brief when a supervised task is active.
                from argus import grok_code
                from argus.see import api as see_api
                grok_msg = see_api.prepare_worker_prompt(conversation_id, message)
                source = grok_code.send(conversation_id, grok_msg, attachments=mats or None)
            else:
                # Local / external: enriched text (OCR + paths) already in model_message.
                # stream_run runs memory_policy.after_turn itself (F1).
                ext = model_config.external(model_name)
                if ext and ext.get("api_key_env") and not model_config.external_api_key(model_name):
                    raise RuntimeError(
                        f"{model_name}: missing {ext['api_key_env']} "
                        f"(add it to ~/.config/secrets/credentials.env and restart argus-ui)")
                def on_event_local(phase, detail, step):
                    if phase == "tool" and detail:
                        tools_for_policy.append(detail)
                    if phase == "silent":
                        turn_flags["silent"] = True
                    if phase == "blocked":
                        turn_flags["blocked"] = True
                    on_event(phase, detail, step)
                source = loop.stream_run(
                    registry, model_message,
                    model_name=(ext["model_id"] if ext else model_name),
                    base_url=(ext["base_url"] if ext else MODEL_URL),
                    api_key=(model_config.external_api_key(model_name) if ext else "none"),
                    turn_budget=8, message_history=history, on_event=on_event_local,
                    enable_thinking=_thinking_for_turn(model_name, model_message),
                    conversation_id=conversation_id)
            async for content in source:
                if isinstance(content, tuple) and content[0] == "__event__":
                    ev = content[1]
                    # Keep the always-on status pill driven on tool use (as before)...
                    if ev.get("kind") == "tool_use":
                        name = ev.get("name", "tool")
                        on_event("tool", name, 0)
                        if model_name in ("claude-code", "grok"):
                            tools_for_policy.append(name)
                    elif ev.get("kind") == "done":   # OAuth-agent metric: turn wall-clock
                        if ev.get("duration_ms"):
                            metrics.CC_DURATION.observe(ev["duration_ms"] / 1000)
                    # ...and forward the rich activity to the tap-to-expand panel
                    # (claude-code / grok paths; the 80B path never yields __event__).
                    publish("bubble_activity", {"id": bubble_id, "event": ev})
                    continue
                final = content
                _turn_touch(conversation_id, "writing", "")
                publish("bubble_update", {"id": bubble_id, "content": content})
            if model_name in ("claude-code", "grok"):
                metrics.CC_TURNS.labels("ok").inc()
                # F1: cold-candidate policy for agent paths (loop.stream_run does local)
                try:
                    from argus import memory_policy as mp
                    await asyncio.to_thread(
                        mp.after_turn,
                        user_message=store_text or message,
                        assistant_text=final or "",
                        tools_called=tools_for_policy,
                        conversation_id=conversation_id,
                    )
                except Exception:
                    pass
            # Turn contract at the publish boundary: strip residual NO_REPLY text.
            # Local stream_run already resolved empty→retry→BLOCKED and may have set
            # turn_flags["silent"] via the sink (empty final text alone is not NO_REPLY).
            n_tools = len(tools_for_policy)
            payload = loop.public_turn_payload(final, n_tools)
            if payload.get("silent"):
                turn_flags["silent"] = True
            if turn_flags["silent"]:
                final = ""
                publish("bubble_update", {"id": bubble_id, "content": ""})
            else:
                final = payload["text"] if payload.get("kind") != "EMPTY" else (final or "")
            terminal = "NO_REPLY" if turn_flags["silent"] else (
                "BLOCKED" if turn_flags["blocked"] else payload.get("kind")
            )
            row = await asyncio.to_thread(
                store.add_message, conversation_id, "assistant", final, model_name)
            publish("bubble_done", {
                "id": bubble_id,
                "db_id": row["id"],
                "user_id": user_row["id"],
                "conversation_id": conversation_id,
                "silent": turn_flags["silent"],
                "terminal": terminal,
            })
        except asyncio.CancelledError:
            if final:  # persist whatever streamed before cancel so memory stays consistent
                await asyncio.to_thread(
                    store.add_message, conversation_id, "assistant", final, model_name)
            publish("bubble_done", {"id": bubble_id, "cancelled": True})
        except Exception as e:
            if model_name in ("claude-code", "grok"):
                metrics.CC_TURNS.labels("error").inc()
            publish("bubble_done", {"id": bubble_id, "error": str(e)})
        finally:
            _turn_end(conversation_id)

    task = asyncio.create_task(run_chat())
    TASKS[bubble_id] = task

    return JSONResponse({"id": bubble_id})


# ── Roundtable ──────────────────────────────────────────────────────────────
# A three-way room: Shane + Gemma (local, via llama-swap) + Claude (via the
# stateless :8100 shim). Unlike /argus/chat this is a PURE chat-completion path
# — no harness loop, no tools. A roundtable is a discussion, not a tool-execution
# turn, so there's no hollow-success surface and replies stay conversational.
# Each addressed model is fed the SAME speaker-labeled transcript so it knows who
# said what and that it's a group chat. For "both", Gemma answers first and is
# persisted, then Claude answers seeing her fresh reply. Streams over the same
# bubble_update/bubble_done SSE the UI already consumes.
ROUNDTABLE_MODELS = {
    "gemma":  {"id": "gemma4-26b", "base": MODEL_URL,
               "base_alt": None, "speaker": "Gemma"},
    "claude": {"id": "claude", "base": "http://127.0.0.1:8100/v1",
               "base_alt": None, "speaker": "Claude"},
}
_RT_SPEAKER_BY_MODEL = {"gemma4-26b": "Gemma", "claude": "Claude"}


def _roundtable_system(speaker: str, other: str) -> str:
    return (
        f"You are {speaker}, one voice at a roundtable with Shane (a human) and "
        f"{other} (another AI). It's a group chat — everyone sees every message, and "
        f"the transcript below is labeled by speaker. Respond ONLY as {speaker}, in "
        f"your own voice: do NOT write lines for Shane or {other}. Be concise and "
        f"conversational. You may agree with, build on, or push back on what {other} "
        f"said, and you can address the room or reply to someone by name. Do not prefix "
        f"your reply with your own name — the interface labels it for you."
    )


def _roundtable_transcript(conversation_id: str) -> str:
    """The shared, speaker-labeled transcript every participant is shown."""
    rows = store.get_messages(conversation_id, limit=40)   # oldest-first
    lines = []
    for r in rows:
        if r["role"] == "user":
            who = "Shane"
        else:
            who = _RT_SPEAKER_BY_MODEL.get(r["model"], r["model"] or "Assistant")
        lines.append(f"{who}: {r['content']}")
    return "\n".join(lines)


async def _stream_completion(base_url: str, model_id: str, messages: list):
    """Stream an OpenAI chat-completion, yielding the ACCUMULATED text so far
    (the contract the UI's bubble_update expects)."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model_id, "messages": messages, "stream": True}
    acc = ""
    timeout = httpx.Timeout(300.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or [{}]
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    acc += delta
                    yield acc


@app.post("/argus/roundtable")
async def roundtable(request: Request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    to = (body.get("to") or "both").lower()
    conversation_id = _cid(body.get("conversation_id"))
    if not message:
        raise HTTPException(400, "empty roundtable message")

    order = ["gemma", "claude"] if to == "both" else [to]
    targets = [t for t in order if t in ROUNDTABLE_MODELS]
    if not targets:
        raise HTTPException(400, "roundtable 'to' must be gemma, claude, or both")

    # Persist Shane's turn first so it's in the transcript every model is shown.
    user_row = await asyncio.to_thread(
        store.add_message, conversation_id, "user", message, None)

    # One bubble per speaker, in speaking order, so the UI can lay out placeholders.
    bubbles = [{"id": uuid.uuid4().hex, "target": t,
                "model": ROUNDTABLE_MODELS[t]["id"],
                "speaker": ROUNDTABLE_MODELS[t]["speaker"]} for t in targets]

    async def run_round():
        _turn_begin(conversation_id, bubbles[0]["id"],
                    model_name=bubbles[0].get("model"))
        try:
            for b in bubbles:
                spec = ROUNDTABLE_MODELS[b["target"]]
                other = "Claude" if spec["speaker"] == "Gemma" else "Gemma"
                # Rebuild the transcript FRESH per speaker so Claude sees Gemma's
                # just-persisted reply when the room was addressed with "both".
                transcript = await asyncio.to_thread(
                    _roundtable_transcript, conversation_id)
                messages = [
                    {"role": "system",
                     "content": _roundtable_system(spec["speaker"], other)},
                    {"role": "user",
                     "content": f"{transcript}\n\n{spec['speaker']}:"},
                ]
                _turn_touch(conversation_id, "writing", spec["speaker"])
                final = ""
                try:
                    async for acc in _stream_completion(
                            spec["base"], spec["id"], messages):
                        final = acc
                        publish("bubble_update", {"id": b["id"], "content": acc})
                except Exception as e:  # one speaker failing must not sink the room
                    publish("bubble_done",
                            {"id": b["id"], "error": f"{spec['speaker']}: {e}"})
                    continue
                row = await asyncio.to_thread(
                    store.add_message, conversation_id, "assistant",
                    final, spec["id"])
                publish("bubble_done", {"id": b["id"], "db_id": row["id"],
                                        "user_id": user_row["id"],
                                        "conversation_id": conversation_id,
                                        "speaker": spec["speaker"]})
        finally:
            _turn_end(conversation_id)

    asyncio.create_task(run_round())
    return JSONResponse({"user_id": user_row["id"], "bubbles": bubbles})


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
    # among real local models), then append the SuperGrok OAuth agent path.
    # `grok` is a separate path — selecting it never hits llama-swap, so it
    # never evicts a resident local model (e.g. the 80B stays loaded). The UI
    # exposes it as the GK toggle overlay, not a dropdown entry.
    def _cfg(mid, backend="argus"):
        # cfg for the picker — display/vision/warm/accent all from the manifest, so a
        # new model is described (and coloured) without touching server.py or app.js.
        return {"display": model_config.display(mid) or mid, "backend": backend,
                "vision": _is_vision(mid), "warm_on_select": _warm_on_select(mid),
                "accent": model_config.accent(mid)}
    entries = []
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            data = (await c.get(f"{_LOCAL_BASE}/v1/models")).json()
        for m in data.get("data", []):
            entries.append([m["id"], _cfg(m["id"])])
    except Exception:
        # llama-swap unreachable — still offer the default so the UI isn't empty.
        entries.append([DEFAULT_MODEL, _cfg(DEFAULT_MODEL)])
    # external (non-llama-swap) models — e.g. the CPU Gemma running on its own ollama
    for name in model_config.externals():
        entries.append([name, _cfg(name)])
    # SuperGrok OAuth agent (subscription via Grok Build CLI — not llama-swap /
    # not console API credits). claude-code remains routable in chat() for old
    # threads but is no longer offered in the picker (swapped for GK).
    entries.append(["grok", _cfg("grok", backend="grok")])
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


# ── NEC (NFPA 70 2023) lookup tab ───────────────────────────────────────────
@app.get("/nec/info")
async def nec_info():
    try:
        return JSONResponse({"pages": nec.page_count()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/nec/render")
async def nec_render(page: int = 0, hl: str = "", dpi: int = 160):
    highlights = [h for h in hl.split("|") if h.strip()] if hl else []
    dpi = max(80, min(int(dpi), 300))
    try:
        data = await asyncio.to_thread(nec.render_page, page, highlights, dpi)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})

@app.post("/nec/ask")
async def nec_ask(request: Request):
    body = await request.json()
    q = (body.get("q") or "").strip()
    if not q:
        return JSONResponse({"error": "empty question"}, status_code=400)
    try:
        return JSONResponse(await nec.ask(q))
    except Exception as e:
        return JSONResponse({"error": str(e), "hits": []}, status_code=500)

@app.post("/nec/translate")
async def nec_translate(request: Request):
    body = await request.json()
    try:
        t = await nec.translate_page(body.get("q", ""), int(body.get("page", 0)),
                                     body.get("section", ""))
        return JSONResponse({"translation": t})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/argus/lane-grants")
async def get_lane_grants():
    """Per-model clearance for the high-blast lanes (shell, code_edit), for the
    permissions widget. Excludes hard-denied models (Loki) and the cloud path."""
    models = []
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            data = (await c.get(f"{_LOCAL_BASE}/v1/models")).json()
        for m in data.get("data", []):
            mid = m["id"]
            if any(d in mid for d in loop.LANE_MODEL_DENY):
                continue  # Loki et al. — never listed, never grantable
            models.append({"id": mid, "display": model_config.display(mid) or mid})
    except Exception:
        pass
    models.sort(key=lambda x: x["display"].lower())
    gates = loop.effective_gates()
    grants = {m["id"]: [ln for ln in loop.TOGGLEABLE_LANES
                        if any(mm in m["id"] for mm in gates.get(ln, set()))]
              for m in models}
    return JSONResponse({"lanes": list(loop.TOGGLEABLE_LANES), "models": models,
                         "grants": grants, "denied": sorted(loop.LANE_MODEL_DENY)})


@app.post("/argus/lane-grants")
async def set_lane_grants(request: Request):
    """Write a full desired-state grants snapshot: {"grants": {model_id: [lanes]}}.
    Denied models are dropped server-side; only TOGGLEABLE_LANES are honored."""
    body = await request.json()
    desired = body.get("grants", {}) or {}
    per_lane = {ln: [] for ln in loop.TOGGLEABLE_LANES}
    for mid, lanes in desired.items():
        if any(d in mid for d in loop.LANE_MODEL_DENY):
            continue
        for ln in (lanes or []):
            if ln in per_lane:
                per_lane[ln].append(mid)
    written = await asyncio.to_thread(loop.save_lane_grants, per_lane)
    return JSONResponse({"ok": True, "grants": written})


@app.get("/argus/tool-grants")
async def get_tool_grants():
    """Per-model, per-TOOL permissions for the widget. Auto-lists EVERY registry tool
    and EVERY served model (Loki included — its gated-lane floor still applies at run
    time), so new tools/models appear with no widget edit."""
    models = []
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            data = (await c.get(f"{_LOCAL_BASE}/v1/models")).json()
        models = [{"id": m["id"], "display": model_config.display(m["id"]) or m["id"]}
                  for m in data.get("data", [])]
    except Exception:
        pass
    models.sort(key=lambda x: x["display"].lower())
    gates = loop.effective_gates()
    denies = loop.LANE_MODEL_DENY
    tools = sorted(registry.all(), key=lambda t: (getattr(t, "provider", ""), t.name))
    tool_meta = [{"name": t.name, "provider": getattr(t, "provider", "native"),
                  "description": (t.description or "")[:120]} for t in tools]

    def eff(mid, t):
        prov = getattr(t, "provider", None)
        denied = any(d in mid for d in denies)
        allowed = True
        if prov in gates:
            allowed = not denied and any(m in mid for m in gates[prov])
        ov = loop._tool_override(mid, t.name)
        if ov is not None:
            allowed = ov
        if denied and prov in gates:
            allowed = False
        return allowed

    effective = {m["id"]: {t.name: eff(m["id"], t) for t in tools} for m in models}
    return JSONResponse({"models": models, "tools": tool_meta,
                         "effective": effective, "overrides": loop._load_tool_grants()})


@app.post("/argus/tool-grants")
async def set_tool_grants(request: Request):
    """Persist per-tool overrides: {"grants": {model_id: {tool_name: bool}}}."""
    body = await request.json()
    written = await asyncio.to_thread(loop.save_tool_grants, body.get("grants", {}) or {})
    return JSONResponse({"ok": True, "grants": written})


@app.post("/argus/see/task")
async def see_task_slash(request: Request):
    """/task slash command — SEE planner + task control (specs/see.md S3)."""
    body = await request.json()
    text = body.get("text") or body.get("message") or ""
    conversation_id = _cid(body.get("conversation_id"))
    from argus.see import slash as see_slash
    try:
        result = await asyncio.to_thread(
            see_slash.handle_task_command, text, conversation_id=conversation_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.get("/argus/see/tasks")
async def see_list_tasks(conversation_id: str | None = None, limit: int = 20):
    """List SEE tasks (optional conversation filter)."""
    from argus.storage import get_store
    cid = _cid(conversation_id) if conversation_id else None
    tasks = await asyncio.to_thread(
        get_store().list_see_tasks,
        conversation_id=cid, include_terminal=True, limit=min(limit, 50),
    )
    return JSONResponse({"tasks": tasks})


@app.get("/argus/see/task/{task_id}")
async def see_get_task(task_id: str):
    from argus.see import api as see_api
    task = await asyncio.to_thread(see_api.get, task_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"task": task.to_dict(), "brief": see_api.worker_brief(task_id)})


@app.post("/argus/make")
async def make_app_endpoint(request: Request):
    """/make slash command — export a code block to an installed desktop app on anvil
    (icon + executable + trusted .desktop launcher). Human-initiated promotion of code.
    Defaults to the fast programmatic icon; ai_icon:true uses Lumen (evicts the LLM)."""
    import subprocess
    import sys
    import tempfile
    body = await request.json()
    name = (body.get("name") or "").strip()
    code = body.get("code") or ""
    desc = (body.get("desc") or "").strip()
    lang = "bash" if body.get("language") == "bash" else "python"
    if not name or not code.strip():
        return JSONResponse({"error": "need a name and a code block"}, status_code=400)
    fd, cf = tempfile.mkstemp(suffix=".txt")
    os.write(fd, code.encode()); os.close(fd)
    try:
        args = [sys.executable, os.path.join(os.path.dirname(__file__), os.pardir,
                                             "scripts", "make_app.py"),
                "--name", name, "--desc", desc or name, "--lang", lang, "--code-file", cf]
        if not body.get("ai_icon"):
            args.append("--no-ai-icon")
        if body.get("terminal") is False:
            args.append("--no-terminal")
        r = await asyncio.to_thread(subprocess.run, args, capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            return JSONResponse({"error": (r.stderr or "make failed")[-500:]}, status_code=500)
        out = json.loads(r.stdout); out["ok"] = True
        return JSONResponse(out)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    finally:
        try: os.unlink(cf)
        except OSError: pass


@app.get("/argus/make/icon")
async def make_app_icon(slug: str):
    p = os.path.expanduser(f"~/.local/share/argus-apps/{os.path.basename(slug)}.png")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/argus/history")
async def get_history(conversation_id: str | None = None, limit: int = 100,
                     before_id: int | None = None):
    msgs = await asyncio.to_thread(
        store.get_messages, _cid(conversation_id), limit, before_id)
    return JSONResponse(msgs)

@app.post("/argus/conversations")
async def create_conversation(request: Request):
    # The "New chat" button POSTed here and got 405 (no handler), so it silently fell back
    # to the default thread — you could never actually start fresh. Conversations are
    # implicit (born on first message), so "new" = a fresh id. Crucially, a new
    # conversation_id also means a FRESH claude-code session (no carried-over context = fast).
    return JSONResponse({"id": uuid.uuid4().hex})


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
