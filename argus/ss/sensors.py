"""Harness-level SS sensors — model-agnostic.

The seated *worker* can be any model the harness drives (llama-swap, Grok,
Claude Code, a models.yaml external). SS must not special-case a worker.

Sources:
  - Generation pulse: begin_turn / mark_activity / end_turn, called from the
    loop driver and the UI turn lifecycle. Not llama-swap /metrics.
  - backend_ok: probe the *current* worker backend (OpenAI-compat URL, or
    OAuth ready). Cached. Unknown ≠ down.
  - gpu_ok: host nvidia-smi, but only when the seated worker actually uses
    the 5080. Cloud / OAuth seats report gpu_ok=True.

All public functions are fail-soft: they never raise into SEE.
"""
from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

OAUTH_MODELS = frozenset({"grok", "claude-code"})
PROBE_TTL = float(os.environ.get("ARGUS_SS_PROBE_TTL", "2"))
PROBE_TIMEOUT = float(os.environ.get("ARGUS_SS_PROBE_TIMEOUT", "0.4"))

_lock = threading.Lock()


class _Pulse:
    model_name: str = ""
    backend: str = ""          # openai | oauth | external
    base_url: str = ""
    generating: bool = False
    streamed: bool = False     # True only after a real token/activity pulse
    last_token_ts: float = 0.0
    gen_start_ts: float = 0.0
    token_count: int = 0


_pulse = _Pulse()
_backend_cache: tuple[float, bool] = (0.0, True)
_gpu_cache: tuple[float, bool] = (0.0, True)

# Injected by tests. None → real probe.
_probe_backend_fn: Callable[[], bool] | None = None
_probe_gpu_fn: Callable[[], bool] | None = None


def reset() -> None:
    """Clear pulse + caches. Tests only."""
    global _backend_cache, _gpu_cache
    with _lock:
        _pulse.model_name = ""
        _pulse.backend = ""
        _pulse.base_url = ""
        _pulse.generating = False
        _pulse.streamed = False
        _pulse.last_token_ts = 0.0
        _pulse.gen_start_ts = 0.0
        _pulse.token_count = 0
        _backend_cache = (0.0, True)
        _gpu_cache = (0.0, True)


def configure(
    *,
    probe_backend: Callable[[], bool] | None = None,
    probe_gpu: Callable[[], bool] | None = None,
) -> None:
    """Replace live probes (tests). Pass None to restore the real probe."""
    global _probe_backend_fn, _probe_gpu_fn
    _probe_backend_fn = probe_backend
    _probe_gpu_fn = probe_gpu


def classify_backend(model_name: str | None, *, base_url: str = "",
                     backend: str = "") -> str:
    """openai | oauth | external. Never branch on a specific local model id."""
    if backend in ("openai", "oauth", "external"):
        return backend
    name = (model_name or "").strip()
    if name in OAUTH_MODELS:
        return "oauth"
    if name:
        try:
            from argus import model_config
            if model_config.external(name):
                return "external"
        except Exception:
            pass
    return "openai"


def uses_host_gpu(model_name: str | None, backend: str = "") -> bool:
    kind = classify_backend(model_name, backend=backend)
    return kind == "openai"


def begin_turn(model_name: str = "", *, backend: str = "",
               base_url: str = "") -> None:
    try:
        now = time.time()
        kind = classify_backend(model_name, base_url=base_url, backend=backend)
        with _lock:
            _pulse.model_name = model_name or ""
            _pulse.backend = kind
            _pulse.base_url = (base_url or "").rstrip("/")
            _pulse.generating = True
            _pulse.streamed = False
            _pulse.last_token_ts = now
            _pulse.gen_start_ts = now
            _pulse.token_count = 0
    except Exception:
        return


def mark_activity(n: int = 1) -> None:
    """A token, thinking chunk, or other worker output. Makes stall detectable."""
    try:
        now = time.time()
        with _lock:
            _pulse.generating = True
            _pulse.streamed = True
            _pulse.last_token_ts = now
            _pulse.token_count += max(1, int(n))
    except Exception:
        return


def end_turn() -> None:
    try:
        with _lock:
            _pulse.generating = False
    except Exception:
        return


def _probe_oauth(model_name: str) -> bool:
    if model_name == "grok":
        try:
            from argus.grok_code import oauth_ready
            return bool(oauth_ready())
        except Exception:
            return True
    if model_name == "claude-code":
        # No cheap ready-check that isn't a process spawn. Unknown ≠ down.
        return True
    return True


def _probe_openai(base_url: str) -> bool:
    url = (base_url or os.environ.get("ARGUS_MODEL_URL") or "").rstrip("/")
    if not url:
        return True
    # Accept either /v1 or a bare host; always hit /models.
    if url.endswith("/v1"):
        probe = url + "/models"
    else:
        probe = url.rstrip("/") + "/v1/models"
    try:
        req = urllib.request.Request(probe, method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            return 200 <= getattr(r, "status", 200) < 500
    except (urllib.error.HTTPError, ) as e:
        # 401/404 still means the process is up.
        return e.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    except Exception:
        return True


def _probe_backend() -> bool:
    if _probe_backend_fn is not None:
        try:
            return bool(_probe_backend_fn())
        except Exception:
            return True
    with _lock:
        kind = _pulse.backend
        name = _pulse.model_name
        url = _pulse.base_url
    if kind == "oauth":
        return _probe_oauth(name)
    return _probe_openai(url)


def _probe_gpu() -> bool:
    if _probe_gpu_fn is not None:
        try:
            return bool(_probe_gpu_fn())
        except Exception:
            return True
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT + 0.3,
        )
        return out.returncode == 0 and bool((out.stdout or "").strip())
    except FileNotFoundError:
        return True  # no nvidia-smi (tests / CPU host) → not a GPU failure
    except Exception:
        return False


def _cached(cache: tuple[float, bool], probe: Callable[[], bool],
            now: float) -> tuple[tuple[float, bool], bool]:
    ts, val = cache
    if now - ts < PROBE_TTL and ts > 0:
        return cache, val
    try:
        val = bool(probe())
    except Exception:
        val = cache[1] if ts > 0 else True
    return (now, val), val


def read(*, now: float | None = None) -> dict[str, Any]:
    """Nested extras for snapshot_from_task. Always returns a full shape."""
    tnow = now if now is not None else time.time()
    global _backend_cache, _gpu_cache
    try:
        with _lock:
            name = _pulse.model_name
            kind = _pulse.backend
            url = _pulse.base_url
            generating = _pulse.generating
            streamed = _pulse.streamed
            last = _pulse.last_token_ts
            start = _pulse.gen_start_ts
            ntok = _pulse.token_count
        since = (tnow - last) if last else 0.0
        elapsed = (tnow - start) if start and generating else 0.0
        tps = (ntok / elapsed) if elapsed > 0.05 and ntok else 0.0
        _backend_cache, backend_ok = _cached(_backend_cache, _probe_backend, tnow)
        gpu_relevant = uses_host_gpu(name, kind)
        if gpu_relevant:
            _gpu_cache, gpu_ok = _cached(_gpu_cache, _probe_gpu, tnow)
        else:
            gpu_ok = True
        service = name or kind or url or "worker"
        return {
            "model": {
                "name": name,
                "generating": generating,
                "tokens_per_second": tps,
                "seconds_since_token": since if generating else 0.0,
                "streamed": streamed,
            },
            "system": {
                "gpu_ok": gpu_ok,
                "backend_ok": backend_ok,
                "service": service,
            },
        }
    except Exception:
        return {
            "model": {"name": "", "generating": False, "tokens_per_second": 0.0,
                      "seconds_since_token": 0.0, "streamed": False},
            "system": {"gpu_ok": True, "backend_ok": True, "service": ""},
        }
