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
    """Save a note to memory so it can be recalled later with lookup_memory. Use this
    to remember a fact, decision, setting, or research finding. Provide the full note
    text (a clear first line becomes its title). The note is recorded as a *proposed*
    memory — it is retrievable immediately but not treated as confirmed truth until it
    has been corroborated; it can later be superseded or invalidated."""
    if not note or not note.strip():
        raise ModelRetry("save_note: empty note. Provide the text to save.")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    first = note.strip().splitlines()[0][:60]
    slug = re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-") or "note"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    fname = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    body = f"{note.strip()}\n\n_saved by argus {stamp}_\n"

    # B2 (specs/memory_system.md §6a/§6b): nothing is born permanent. Every durable save
    # enters the fact lifecycle as `proposed`, carrying provenance — never a god-mode
    # instant-durable write that would bypass supersession/invalidation/audit. The fact
    # row is the source of truth; the note file + live index are the recall substrate,
    # kept in sync so lookup_memory still finds it (recall must not regress).
    from ..storage import get_store
    fact = get_store().add_fact(key=first, value=note.strip(), source="save_note")

    (NOTES_DIR / fname).write_text(body)
    try:                                  # make it searchable right now (best-effort)
        from .. import memory
        memory.add_note(fname, body, fact_id=fact["id"])   # link chunk → lifecycle fact
    except Exception:
        pass
    return {"saved": True, "state": fact["state"], "fact_id": fact["id"], "file": fname}


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
