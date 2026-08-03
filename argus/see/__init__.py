"""Supervisory Execution Engine (SEE) — specs/see.md.

Public surface:
  from argus.see import api
  task = api.create("Install X", success_criteria=[...], checklist=[...])
  api.on_tool(task.id, "shell", args={...})
  api.checkpoint(task.id, "container up", checklist_item="...")
  api.add_evidence(task.id, criterion, summary, kind="http")
  api.request_verify(task.id)
"""
from . import api, engine, models

__all__ = ["api", "engine", "models"]
