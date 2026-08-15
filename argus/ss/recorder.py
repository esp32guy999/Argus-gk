"""Append-only SS observation log — the v1 training/eval dataset.

Writes one JSON line per observe(). Path:
  ARGUS_SS_LOG if set, else <dir of ARGUS_DB>/ss.jsonl, else disabled.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def log_path() -> Path | None:
    explicit = os.environ.get("ARGUS_SS_LOG")
    if explicit:
        return Path(explicit)
    db = os.environ.get("ARGUS_DB")
    if db:
        return Path(db).expanduser().resolve().parent / "ss.jsonl"
    return None


def record(snapshot: dict[str, Any], decision: dict[str, Any],
           *, outcome: str | None = None) -> None:
    path = log_path()
    if path is None:
        return
    line = {
        "ts": time.time(),
        "snapshot": snapshot,
        "decision": decision,
    }
    if outcome is not None:
        line["outcome"] = outcome
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except OSError:
        return
