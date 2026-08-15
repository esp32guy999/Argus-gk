"""Optional SS judgment head — Z-Engineer Qwen3-4B, thinking off.

Only called when rules return None *and* the snapshot is suspicious.
Disabled unless ARGUS_SS_URL is set. Never put this model on llama-swap
(that would evict the GPU primary). Serve it on a dedicated CPU port.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .protocol import SsDecision, normalize_action, normalize_reason
from .snapshot import Snapshot

# Winner of the 2026-08-14 SS bake-off (action acc 4/6, least interruptive).
WINNER = "z-engineer-qwen3-4b"
WINNER_PATH = "/home/shane/models/z-engineer/Qwen3-4b-Z-Image-Engineer-V4-Q5_K_M.gguf"
DEFAULT_TIMEOUT = float(os.environ.get("ARGUS_SS_MODEL_TIMEOUT", "5"))

SYSTEM = (
    "You are a task supervisor, not a chat assistant. "
    "Given a JSON state snapshot, respond with ONLY a single JSON object, "
    "no markdown, no thinking text. "
    'Schema: {"action": <enum>, "reason_code": <enum>}\n'
    "action enum: NO_OP | WAIT | RETRY | REPLAN | VERIFY | ABORT | ASK_USER\n"
    "reason_code enum: PRODUCTIVE_PROGRESS | NORMAL_WAIT | INFERENCE_STALL | "
    "REPEATED_TOOL_CALL | NO_PROGRESS | LOOP_DETECTED | OBJECTIVE_COMPLETE | "
    "OBJECTIVE_BLOCKED | RESOURCE_FAILURE | UNKNOWN\n"
    "Rules: activity is not progress. Prefer NO_OP when new evidence or state "
    "change toward goal. Prefer REPLAN or RETRY when tools repeat with no new "
    "evidence. WAIT/ABORT for inference or resource failure. ASK_USER when "
    "blocked on an external dependency. VERIFY only when the objective claims "
    "complete or evidence is insufficient — never to interrupt productive work."
)


def ss_url() -> str:
    return (os.environ.get("ARGUS_SS_URL") or "").rstrip("/")


def enabled() -> bool:
    return bool(ss_url())


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _http_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ask_model(
    snap: Snapshot,
    *,
    http_json: Callable[..., dict] | None = None,
    timeout: float | None = None,
) -> SsDecision | None:
    """Call the CPU judgment head. None on any failure (caller falls back)."""
    base = ss_url()
    if not base and http_json is None:
        return None
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    post = http_json or _http_json
    t0 = time.time()
    try:
        raw = post(
            f"{base}/v1/chat/completions" if base else "",
            {
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps(snap.to_dict())},
                ],
                "temperature": 0,
                "max_tokens": 96,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout,
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    except Exception:
        return None
    lat = (time.time() - t0) * 1000
    try:
        msg = (raw.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return None
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    obj = _extract_json(content) or _extract_json(reasoning)
    if not obj:
        return None
    action = normalize_action(obj.get("action"))
    reason = normalize_reason(obj.get("reason_code") or obj.get("reason"))
    if not action or not reason:
        return None
    return SsDecision(
        action, reason, source="model",
        latency_ms=lat, raw=(content or reasoning)[:240],
        details={"winner": WINNER},
    )
