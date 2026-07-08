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
    hits = idx.search(query, k=5)
    return hits or [{"note": "no matching homelab facts found for that query"}]


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
    return {"name": parts[4], "temperature_c": int(parts[0]),
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
            name="start_background_task",
            description=("Delegate a long/multi-step job to run in the background; the "
                         "user is notified on their phone when it finishes. Use for work "
                         "too big to do inline. Returns a task id immediately."),
            tags=["task", "background", "async", "delegate", "long", "job"],
            func=start_background_task,
            example={"task": "Research the best PETG settings for the P1S and save a note."},
        ),
    ]
