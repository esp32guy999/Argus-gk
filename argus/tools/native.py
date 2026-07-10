"""Native tool lane — small, pure-compute / latency-critical tools.

Each is a plain function with type hints + a docstring (Pydantic AI builds the
model-facing schema from these). Raise ModelRetry with a corrective message to
TEACH the model on failure instead of returning an opaque error.
"""
from __future__ import annotations
import datetime as _dt

from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool


def get_time(timezone: str = "UTC") -> str:
    """Return the current date and time (UTC clock). `timezone` is informational."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S %Z")


def calc(expression: str) -> float:
    """Evaluate a simple arithmetic expression, e.g. '3 * (4 + 1)'.
    Only digits, spaces and + - * / ( ) . are allowed."""
    allowed = set("0123456789+-*/(). ")
    if not expression or set(expression) - allowed:
        raise ModelRetry(
            "calc only accepts digits and + - * / ( ) . — "
            f"got {expression!r}. Rewrite the expression."
        )
    try:
        return float(eval(expression, {"__builtins__": {}}, {}))  # empty namespace
    except Exception as e:
        raise ModelRetry(f"calc could not evaluate {expression!r}: {e}")


def lookup_memory(query: str) -> list:
    """Search the homelab knowledge base for facts relevant to the query.

    The knowledge base is the curated docs (hosts, IPs, ports, hostnames, service
    endpoints, file paths, credentials, hardware quirks, operational notes). Use this
    whenever you need a specific homelab detail instead of guessing — IPs, ports,
    paths, and quirks must come from here, never from memory. Returns the most
    relevant snippets, each tagged with its source doc."""
    from .. import memory
    idx = memory.get_index()
    if idx is None:
        raise ModelRetry(
            "lookup_memory: the knowledge index is unavailable (embeddings server "
            "down?). Answer from the current context or say you don't know — do not "
            "invent IPs/ports/paths."
        )
    # State-aware recall (specs/memory_system.md §6a). Over-fetch, then resolve each
    # note-backed hit's LIVE lifecycle state: drop invalidated/superseded (a known-false
    # or replaced memory is never recalled as truth), flag proposed/observed as
    # unconfirmed so the agent doesn't treat a guess as fact. Curated docs (no fact_id)
    # pass through untouched. Over-fetching keeps the result count up after drops.
    hits = idx.search(query, k=12)
    store = None
    out: list = []
    for h in hits:
        fid = h.get("fact_id")
        if fid:
            if store is None:
                from ..storage import get_store
                store = get_store()
            fact = store.get_fact(fid)
            if fact is not None:
                st = fact["state"]
                if st in ("invalidated", "superseded"):
                    continue
                h = {**h, "state": st}
                if st in ("proposed", "observed"):
                    h["unconfirmed"] = True
        out.append(h)
        if len(out) >= 5:
            break
    return out or [{"note": "no matching homelab facts found for that query"}]


def get_gpu() -> dict:
    """Current GPU status on the Argus host: temperature (°C), utilization (%), and
    VRAM used/total (MiB). Reads nvidia-smi directly. Use for GPU temperature, whether
    the GPU is hot/throttling, or how much VRAM is free — hardware/thermal checks."""
    import subprocess
    q = "temperature.gpu,utilization.gpu,memory.used,memory.total,name"
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise ModelRetry("get_gpu: nvidia-smi not found — no NVIDIA GPU on this host.")
    except subprocess.TimeoutExpired:
        raise ModelRetry("get_gpu: nvidia-smi timed out.")
    if out.returncode != 0:
        raise ModelRetry(f"get_gpu: nvidia-smi failed: {(out.stderr or '').strip()[:200]}")
    line = next((r for r in (out.stdout or "").splitlines() if r.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        raise ModelRetry(f"get_gpu: unexpected nvidia-smi output: {line!r}")
    temp_c = int(parts[0])
    return {"name": parts[4], "temperature_c": temp_c,
            "temperature_f": round(temp_c * 9 / 5 + 32),   # both units so cross-tool compares don't trip
            "utilization_pct": int(parts[1]),
            "vram_used_mib": int(parts[2]), "vram_total_mib": int(parts[3])}


def get_disk(path: str = "/") -> dict:
    """Free/used disk space for the filesystem containing `path` (default '/') on the
    Argus host. Use for how much disk space is free, whether the disk is full, or
    storage capacity. Returns total/used/free in GB and percent used."""
    import shutil
    try:
        total, used, free = shutil.disk_usage(path)
    except (FileNotFoundError, OSError) as e:
        raise ModelRetry(f"get_disk: cannot read disk usage for {path!r}: {e}")
    gb = lambda b: round(b / 1e9, 1)
    return {"path": path, "total_gb": gb(total), "used_gb": gb(used), "free_gb": gb(free),
            "percent_used": round(used / total * 100) if total else 0}


def get_ha_state(query: str) -> dict:
    """Read the current state of a Home Assistant entity by fuzzy name OR entity_id.
    Accepts 'porch lights', 'switch.porch_lights', or just 'porch' — resolves to the
    real entity and returns its state + friendly name. Use to check whether a light /
    switch / sensor / device is on/off or to read any HA value. ONE forgiving call — no
    need to guess the exact entity_id or friendly name (avoids trial-and-error)."""
    import os
    import re
    import httpx
    base = os.environ.get("HA_URL", "http://nyx:8123").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        raise ModelRetry("get_ha_state: HA_TOKEN is not configured on this host.")
    try:
        r = httpx.get(f"{base}/api/states",
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r.raise_for_status()
        states = r.json()
    except httpx.HTTPError as e:
        raise ModelRetry(f"get_ha_state: could not reach Home Assistant ({e}).")

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    q = norm(query)
    qwords = set(q.split())
    scored = []
    for s in states:
        eid = s.get("entity_id", "")
        fn = s.get("attributes", {}).get("friendly_name", "")
        cands = {norm(eid), norm(eid.split(".", 1)[-1]), norm(fn)}
        if query.lower() == eid.lower():
            score = 100
        elif q in cands:
            score = 60
        elif any(c and (q in c or c in q) for c in cands):
            score = 30
        elif qwords and any(c and qwords.issubset(set(c.split())) for c in cands):
            score = 20
        else:
            score = 0
        if score:
            # a "what's the state" query usually means a DEVICE, not an automation/scene
            domain = eid.split(".", 1)[0]
            if domain in ("automation", "scene", "script", "zone", "group", "person"):
                score -= 5
            elif domain in ("light", "switch", "sensor", "binary_sensor", "climate",
                            "lock", "cover", "fan", "media_player"):
                score += 5
            scored.append((score, s))
    if not scored:
        raise ModelRetry(f"get_ha_state: no Home Assistant entity matched {query!r}. "
                         "Try the device's friendly name or its entity_id.")
    scored.sort(key=lambda x: x[0], reverse=True)

    def fmt(s):
        a = s.get("attributes", {})
        return {"entity_id": s["entity_id"], "name": a.get("friendly_name"),
                "state": s.get("state")}
    out = fmt(scored[0][1])
    extra = [fmt(s) for sc, s in scored[1:4] if sc >= 30]
    if extra:
        out["other_matches"] = extra
    return out


def start_background_task(task: str) -> dict:
    """Delegate a long-running or multi-step job to run in the BACKGROUND, detached.
    It runs on its own and the user is notified on their phone when it finishes. Use
    this for work that would take a while (extended research, multi-step jobs) instead
    of attempting a huge job inline. Returns a task id immediately — do NOT wait for
    it; tell the user it has started and you'll notify them."""
    from .. import tasks
    mgr = tasks.manager()
    if mgr is None:
        raise ModelRetry(
            "start_background_task is unavailable here (no background runner). Do the "
            "task inline if you can, or tell the user it can't be backgrounded."
        )
    tid = mgr.submit(task)
    return {"started": True, "task_id": tid,
            "note": "running in background; the user will be notified when it finishes"}


def tools() -> list[Tool]:
    """Provider entry point: emit native tools in the uniform contract."""
    return [
        Tool(
            name="get_time",
            description="Get the current UTC date and time.",
            tags=["time", "utility"],
            func=get_time,
            example={"timezone": "UTC"},
        ),
        Tool(
            name="calc",
            description="Evaluate a simple arithmetic expression.",
            tags=["math", "utility"],
            func=calc,
            example={"expression": "3 * (4 + 1)"},
        ),
        Tool(
            name="lookup_memory",
            description=("Search the homelab knowledge base (curated docs: hosts, IPs, "
                         "ports, services, paths, hardware quirks, ops notes) for facts "
                         "relevant to a query. Use before guessing any homelab detail."),
            tags=["memory", "knowledge", "homelab", "recall", "lookup", "facts", "infra"],
            func=lookup_memory,
            example={"query": "what is nyx's IP address"},
        ),
        Tool(
            name="get_gpu",
            description=("Current GPU status on the Argus host: temperature (°C), "
                         "utilization (%), and VRAM used/total. Use for GPU temperature, "
                         "whether the GPU is hot, or how much VRAM is free."),
            tags=["gpu", "temperature", "vram", "hardware", "nvidia", "sensor",
                  "status", "system", "thermal"],
            func=get_gpu,
            example={},
        ),
        Tool(
            name="get_disk",
            description=("Free and used disk space for the filesystem containing a path "
                         "(default '/') on the Argus host. Use for how much disk space is "
                         "free, whether the disk is full, or storage capacity."),
            tags=["disk", "storage", "space", "free", "filesystem", "capacity",
                  "df", "system"],
            func=get_disk,
            example={"path": "/"},
        ),
        Tool(
            name="get_ha_state",
            description=("Read a Home Assistant entity's current state by fuzzy name or "
                         "entity_id (e.g. 'porch lights', 'switch.porch_lights', 'porch'). "
                         "Use to check if a light/switch/sensor/device is on/off or read "
                         "any HA value — one forgiving call, no need to guess the exact id."),
            tags=["home assistant", "ha", "entity", "state", "light", "switch", "sensor",
                  "device", "on", "off", "status", "is", "smart home"],
            func=get_ha_state,
            example={"query": "porch lights"},
        ),
        Tool(
            name="start_background_task",
            description=("Delegate a long/multi-step job to run in the background; the "
                         "user is notified on their phone when it finishes. Use for work "
                         "too big to do inline. Returns a task id immediately."),
            tags=["task", "background", "async", "delegate", "long", "job"],
            func=start_background_task,
            example={"task": "Research the best PETG settings for the P1S and save a note."},
        ),
    ]
