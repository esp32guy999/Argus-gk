"""Inbox chat contract helpers (specs/chat-client.md).

Keep this module free of FastAPI / the live server so tests can import it
offline. The UI server uses these to normalize `to` and decide who can
join a room.
"""
from __future__ import annotations

import re

# Legacy Claude Code is still routable via the old `model` field, but is
# not an inviteable participant. Media models (z-*) are never in the room.
NOT_INVITEABLE = frozenset({"claude-code"})


def is_inviteable(model_id: str | None) -> bool:
    if not model_id:
        return False
    if model_id.startswith("z-"):
        return False
    if model_id in NOT_INVITEABLE:
        return False
    return True


def normalize_to(body: dict, default_model: str) -> list[str]:
    """Addressed model ids for this send.

    `to` (list or string) wins. If omitted, fall back to `model` / default
    — even if that model is not inviteable, so old clients keep working.
    Empty `to` after filtering inviteable ids is returned as [].
    """
    raw = (body or {}).get("to")
    if raw is None:
        mid = (body or {}).get("model") or default_model
        return [mid] if mid else []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for m in raw:
        if not m or m in seen:
            continue
        if not is_inviteable(m):
            continue
        seen.add(m)
        out.append(str(m))
    return out


def normalize_client_msg_id(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


_TOOL_PLACEHOLDER = re.compile(r"^_\(running `[^`]+`[.…]*\)_\s*$")


def assistant_persist_text(
    final: str | None,
    *,
    error: str | None = None,
    cancelled: bool = False,
    n_tools: int = 0,
) -> str:
    """Text that MUST be stored as the assistant row when a turn ends.

    The inbox client never paints live tokens — if we skip add_message, the
    bubble is gone forever. Empty/placeholder-only bodies after real work
    are the Grok wrap-up ghost.
    """
    text = str(final or "").strip()
    if text and _TOOL_PLACEHOLDER.match(text):
        text = ""
    if cancelled:
        return text or "[Cancelled]"
    if error:
        err = str(error).strip() or "turn failed"
        if text:
            return f"{text}\n\n[Error: {err}]"
        return f"[Error: {err}]"
    if text:
        return text
    if n_tools:
        return f"_(Finished {n_tools} tool call(s) with no wrap-up.)_"
    return "[No reply]"
