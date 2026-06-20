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
    ]
