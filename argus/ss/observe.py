"""SS observe — rules first, model only on leftover suspicion, never authority."""
from __future__ import annotations

import os
import time
from typing import Any

from . import model as ss_model
from . import recorder
from .protocol import SsDecision
from .rules import apply_rules, is_suspicious
from .snapshot import Snapshot, snapshot_from_task


def enabled() -> bool:
    v = (os.environ.get("ARGUS_SS") or "1").strip().lower()
    return v not in ("0", "false", "off", "no")


def _fallback() -> SsDecision:
    # Conservative: do not interrupt if we cannot classify.
    return SsDecision("NO_OP", "UNKNOWN", source="fallback")


def observe(snap: Snapshot | dict, *, now: float | None = None,
            record: bool = True) -> SsDecision:
    """Classify one snapshot. Always returns a decision. Never mutates a task."""
    t0 = time.time()
    snapshot = snap if isinstance(snap, Snapshot) else Snapshot.from_dict(snap)
    decided = apply_rules(snapshot)
    if decided is None:
        if is_suspicious(snapshot):
            decided = ss_model.ask_model(snapshot) or _fallback()
        else:
            decided = SsDecision("NO_OP", "UNKNOWN", source="rules")
    if decided.latency_ms is None:
        decided.latency_ms = (time.time() - t0) * 1000
    if record:
        recorder.record(snapshot.to_dict(), decided.to_dict())
    try:
        from argus import metrics
        metrics.SS_DECISIONS.labels(
            action=decided.action, source=decided.source,
        ).inc()
    except Exception:
        pass
    return decided


def _merge_extras(extras: dict | None) -> dict:
    """Live sensors first; caller extras win (tests / explicit overrides)."""
    live: dict = {}
    try:
        from . import sensors
        live = sensors.read() or {}
    except Exception:
        live = {}
    if not extras:
        return live
    # Shallow-merge nested model/system so a test can override one slice.
    out = dict(live)
    for key in ("model", "system"):
        if isinstance(extras.get(key), dict) or isinstance(live.get(key), dict):
            merged = dict(live.get(key) or {})
            merged.update(extras.get(key) or {})
            out[key] = merged
    for k, v in extras.items():
        if k not in ("model", "system"):
            out[k] = v
    return out


def observe_task(task: Any, *, extras: dict | None = None,
                 now: float | None = None, record: bool = True) -> SsDecision:
    return observe(snapshot_from_task(task, extras=_merge_extras(extras), now=now),
                   now=now, record=record)
