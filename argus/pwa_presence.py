"""PWA focus heartbeat — used to suppress reply toasts while the app is on screen.

The phone PWA freezes JS when minimized, so the server cannot ask "are you looking?"
at reply time. The client reports visibility (and a keepalive ping on hide); we treat
the PWA as focused only if the last report said visible AND is still fresh.
"""
from __future__ import annotations

TTL_SEC = 45.0

_state = {"visible": False, "ts": 0.0}


def reset() -> None:
    _state["visible"] = False
    _state["ts"] = 0.0


def note(visible: bool, now: float) -> dict:
    _state["visible"] = bool(visible)
    _state["ts"] = float(now)
    return snapshot()


def snapshot() -> dict:
    return {"visible": bool(_state["visible"]), "ts": float(_state["ts"])}


def is_focused(now: float, ttl: float = TTL_SEC) -> bool:
    if now - _state["ts"] > ttl:
        return False
    return bool(_state["visible"])


def preview(text: str, limit: int = 120) -> str:
    """One-line toast body; empty means 'do not notify'."""
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)].rstrip() + "…"
