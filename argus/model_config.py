"""Model manifest loader — the single source of per-model config (config/models.yaml).

Consolidates what used to be ~7 scattered dicts (ui/server.py's MODEL_DISPLAY /
VISION_MODELS / WARM_ON_SELECT / CHAT_THINKING / CONTEXT_WINDOW / EXTERNAL_MODELS
and app.js's MODEL_ACCENT). A model absent from the manifest resolves to safe
defaults, so it's usable on first load without any code edit.

Resolution is EXACT-key first, then substring (a served id embedding a short key
still matches, like the lane gates) — exact-first so e.g. `claude-code` never
resolves to the shorter `claude` entry. Hot-reloaded via mtime.
"""
from __future__ import annotations

import os
import threading

import yaml

_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config", "models.yaml")
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None

# Field defaults for a model with no (or partial) manifest entry.
_DEFAULTS = {
    "display": None,     # None → caller falls back to the raw model id
    "accent": None,      # None → frontend palette fallback
    "vision": False,
    "thinking": None,    # None → server's default thinking policy
    "context": 8192,
    "warm": False,
    "external": None,    # {base_url, model_id} for non-llama-swap backends
    "grant": [],         # extra dangerous lanes this model is born cleared for
}


def _load() -> dict:
    global _cache
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        return {"defaults": {}, "models": {}}
    with _lock:
        if _cache is None or _cache[0] != mtime:
            try:
                data = yaml.safe_load(open(_PATH, encoding="utf-8")) or {}
            except Exception:
                data = {"defaults": {}, "models": {}}
            _cache = (mtime, data)
        return _cache[1]


def _resolve(model_id: str | None) -> dict:
    """Merged config for a model: defaults ← file defaults ← the model's entry."""
    data = _load()
    merged = {**_DEFAULTS, **(data.get("defaults") or {})}
    models = data.get("models") or {}
    spec = None
    if model_id in models:                       # exact wins
        spec = models[model_id]
    else:                                        # substring fallback
        for key, val in models.items():
            if key and key in (model_id or ""):
                spec = val
                break
    if isinstance(spec, dict):
        merged.update({k: v for k, v in spec.items() if v is not None or k == "thinking"})
    return merged


# ── Public accessors ─────────────────────────────────────────────────────────
def display(model_id: str) -> str | None:
    return _resolve(model_id)["display"]


def accent(model_id: str) -> str | None:
    return _resolve(model_id)["accent"]


def is_vision(model_id: str) -> bool:
    return bool(_resolve(model_id)["vision"])


def thinking(model_id: str):
    return _resolve(model_id)["thinking"]


def context_window(model_id: str) -> int:
    return int(_resolve(model_id)["context"] or 8192)


def warm_on_select(model_id: str) -> bool:
    return bool(_resolve(model_id)["warm"])


def external(model_id: str) -> dict | None:
    """{base_url, model_id} for a non-llama-swap backend, or None."""
    return _resolve(model_id)["external"]


def grants(model_id: str) -> list[str]:
    """Dangerous lanes this model is born cleared for (unioned into the gates)."""
    return list(_resolve(model_id)["grant"] or [])


def externals() -> dict:
    """All models declaring an `external` backend → {id: {base_url, model_id, display}}."""
    data = _load()
    out = {}
    for mid, spec in (data.get("models") or {}).items():
        ext = (spec or {}).get("external") if isinstance(spec, dict) else None
        if ext:
            out[mid] = {**ext, "display": (spec.get("display") or mid)}
    return out


def all_grants() -> dict:
    """{model_id: [lanes]} for every model with a non-empty `grant`."""
    data = _load()
    out = {}
    for mid, spec in (data.get("models") or {}).items():
        g = (spec or {}).get("grant") if isinstance(spec, dict) else None
        if g:
            out[mid] = list(g)
    return out
