"""Eval harness — dry-run wrapping over the real registry.

Builds the production registry, then rewraps every tool so evals can run against
a live model without touching the homelab: DEFAULT-DENY — every tool is dry-run
(recorded + canned response) unless its name is in SAFE_LIVE (read-only, cheap,
deterministic-enough). New lanes are therefore safe in evals automatically.

Every call (live or dry) is recorded to `calls` so graders can assert on
tool name + args regardless of execution mode.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Read-only tools allowed to execute for real during live evals.
SAFE_LIVE = {
    "get_time", "calc", "lookup_memory",
    "weather_current", "weather_forecast", "get_weather",
    "radarr_getSystemStatus", "radarr_listMovies", "radarr_lookupMovie",
    "sonarr_getSystemStatus", "sonarr_listSeries", "sonarr_lookupSeries",
    "readarr_getSystemStatus", "readarr_listBooks",
    "lidarr_getSystemStatus", "lidarr_listArtists", "lidarr_lookupArtist",
    "prowlarr_getSystemStatus", "prowlarr_listIndexers",
    "navidrome_list_playlists", "navidrome_list_genres",
    "list_dir", "stat_path",
    "GetDateTime", "GetLiveContext", "todo_get_items",
}

# Canned dry-run responses where the generic shape would confuse the model.
CANNED: dict[str, Any] = {
    "HassTurnOn": {"ok": True, "dryrun": True, "result": "device turned on"},
    "HassTurnOff": {"ok": True, "dryrun": True, "result": "device turned off"},
    "HassListAddItem": {"ok": True, "dryrun": True, "result": "item added to list"},
    "web_search": {"dryrun": True, "results": [
        {"title": "Example result", "url": "https://example.com",
         "snippet": "A relevant snippet for the query."}]},
    "web_fetch": {"dryrun": True, "content": "Example page content."},
    "save_note": {"ok": True, "dryrun": True, "result": "note saved"},
    "start_background_task": {"ok": True, "dryrun": True,
                              "result": "background task started (id: eval-dry-1)"},
    "audiobook_search": {"dryrun": True, "results": [
        {"title": "Example Audiobook", "author": "A. Writer", "id": "abb-1"}]},
    "prowlarr_searchReleases": {"dryrun": True, "results": [
        {"title": "Example.Release.2160p", "indexer": "example", "seeders": 42}]},
}


@dataclass
class CallLog:
    calls: list[dict] = field(default_factory=list)

    def record(self, tool: str, kwargs: dict, mode: str):
        self.calls.append({"tool": tool, "args": kwargs, "mode": mode})

    def called(self, tool: str) -> list[dict]:
        return [c for c in self.calls if c["tool"] == tool]

    def args_contain(self, tool: str, needle: str) -> bool:
        needle = needle.lower()
        return any(needle in json.dumps(c["args"]).lower() for c in self.called(tool))

    def reset(self):
        self.calls.clear()


def wrap_registry(reg, log: CallLog):
    """Mutate the registry's tools in place: record every call; dry-run anything
    not in SAFE_LIVE. Tool._wrapped() reads self.func at as_pydantic_tool() time
    (per turn), so in-place mutation is honored."""
    for t in reg.all():
        t.func = _make_runner(t, log)
    return reg


def _make_runner(tool, log: CallLog):
    import functools
    real, name = tool.func, tool.name
    live = name in SAFE_LIVE

    # functools.wraps (same pattern as registry._wrapped) sets __wrapped__, so
    # Pydantic AI resolves the signature AND type hints against the real
    # function's module globals. Hand-copying __annotations__ breaks on tools
    # whose hints reference names (Optional, ...) not imported here.
    @functools.wraps(real)
    def runner(*args, **kwargs):
        log.record(name, kwargs or (args and {"_args": list(args)}) or {}, "live" if live else "dry")
        if live:
            return real(*args, **kwargs)
        return CANNED.get(name, {"ok": True, "dryrun": True,
                                 "result": f"{name} executed (dry-run)"})

    return runner


def toolset_cost(tools) -> dict:
    """Context cost of offering this toolset: count + rough schema tokens
    (chars/4 over name+description+schema/signature)."""
    chars = 0
    for t in tools:
        chars += len(t.name) + len(t.description or "")
        if t.schema is not None:
            chars += len(json.dumps(t.schema))
        else:
            import inspect
            try:
                chars += len(str(inspect.signature(t.func)))
            except (ValueError, TypeError):
                pass
    return {"count": len(tools), "schema_tokens_est": chars // 4}
