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
    ]
