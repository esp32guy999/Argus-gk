"""Notes lane — let the agent persist durable notes it can recall later.

Notes are Argus-OWNED markdown files under ~/argus/notes/ (kept separate from the
human-curated docs so the agent never edits those). The memory lane indexes this
dir, so a saved note is retrievable via lookup_memory; save_note also appends it to
the live in-memory index so it's findable immediately, no restart needed.

This closes the research->save loop: web_search/web_fetch find it, save_note keeps it.
"""
from __future__ import annotations

import os
import pathlib
import re
import time

from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

NOTES_DIR = pathlib.Path(
    os.environ.get("ARGUS_NOTES_DIR", os.path.expanduser("~/argus/notes")))


def save_note(note: str) -> dict:
    """Save a durable note to the knowledge base so it can be recalled later with
    lookup_memory. Use this to remember a fact, decision, setting, or research
    finding the user wants kept. Provide the full note text (a clear first line
    becomes its title)."""
    if not note or not note.strip():
        raise ModelRetry("save_note: empty note. Provide the text to save.")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    first = note.strip().splitlines()[0][:60]
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-") or "note"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    fname = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    body = f"{note.strip()}\n\n_saved by argus {stamp}_\n"
    (NOTES_DIR / fname).write_text(body)
    try:                                  # make it searchable right now
        from .. import memory
        memory.add_note(fname, body)
    except Exception:
        pass
    return {"saved": True, "file": fname}


def tools() -> list[Tool]:
    return [
        Tool(
            name="save_note",
            description=("Save a durable note to the knowledge base (a fact, setting, "
                         "decision, or research finding). Recall it later with "
                         "lookup_memory. Provide the full note text."),
            tags=["notes", "save", "remember", "write", "memory", "knowledge"],
            func=save_note,
            example={"note": "PETG on the P1S: nozzle 240C, bed 70C, dry 65C/6h."},
        )
    ]
