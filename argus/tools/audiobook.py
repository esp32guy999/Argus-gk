"""Audiobook lane — search AudiobookBay (+ Prowlarr) and grab audiobooks.

Thin client over Shane's existing **audiobook-getter** service (nyx:8078) — a working
FastAPI app that fans out search to ABB + Prowlarr, scrapes ABB info-hashes into
magnets, and hands grabs to qBittorrent (which lands them in /audiobooks for
Audiobookshelf to scan). Argus does NOT reimplement any of that — it calls the
service. (audiobook-getter is Shane's own app, not Hermes; nothing is ported.)

Tools:
- audiobook_search(query): browse available audiobooks (title, size, seeders, source).
- audiobook_get(query): pick the best AUDIOBOOK result and start the download in one
  call. Prefers real audiobooks (ABB) over ebook/PDF results, then by seeders.

Endpoint via ARGUS_AUDIOBOOK_URL (default http://nyx:8078). No auth (internal service).
"""
from __future__ import annotations

import os
import re

import httpx
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

BASE = os.environ.get("ARGUS_AUDIOBOOK_URL", "http://nyx:8078").rstrip("/")
_EBOOK = re.compile(r"\b(epub|pdf|mobi|azw3?|ebook|e-book|kindle)\b", re.I)


def _search(query: str) -> list[dict]:
    try:
        r = httpx.get(f"{BASE}/api/search", params={"q": query}, timeout=40)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ModelRetry(f"audiobook search failed (is audiobook-getter up on {BASE}?): {e}")
    return (r.json() or {}).get("results", [])


def _is_audiobook(item: dict) -> bool:
    """Prefer real audiobooks: ABB source, or not an obvious ebook by title."""
    if "abb" in (item.get("source_type") or "").lower():
        return True
    return not _EBOOK.search(item.get("title") or "")


def _rank(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (_is_audiobook(x), x.get("seeders") or 0), reverse=True)


def audiobook_search(query: str) -> list:
    """Search for audiobooks (AudiobookBay + Prowlarr) by title or author. Returns the
    top matches with title, size, seeders and source. To actually download one, use
    audiobook_get."""
    ranked = _rank(_search(query))
    if not ranked:
        raise ModelRetry(f"no audiobooks found for '{query}'. Try a different title/author.")
    return [{"title": i.get("title"), "size": i.get("size_human"),
             "seeders": i.get("seeders"), "source": i.get("source"),
             "audiobook": _is_audiobook(i)} for i in ranked[:8]]


def audiobook_get(query: str) -> dict:
    """Find and DOWNLOAD an audiobook by title/author in one step. Picks the best
    audiobook match (prefers real audiobooks over ebooks, then most seeders) and starts
    the download; it lands in the audiobook library once complete. Downloading is
    asynchronous — report it's downloading, not ready."""
    ranked = _rank(_search(query))
    if not ranked:
        raise ModelRetry(f"no audiobooks found for '{query}'. Try a different title/author.")
    best = ranked[0]
    try:
        r = httpx.post(f"{BASE}/api/grab", json=best, timeout=40)
        r.raise_for_status()
        result = r.json()
    except httpx.HTTPError as e:
        raise ModelRetry(f"audiobook grab failed: {e}")
    if not result.get("ok", True):
        raise ModelRetry(f"audiobook grab rejected: {result.get('error', 'unknown error')}")
    return {"grabbed": True, "title": best.get("title"), "source": best.get("source"),
            "seeders": best.get("seeders"),
            "note": "downloading; it will appear in the audiobook library once imported"}


def tools() -> list[Tool]:
    return [
        Tool(
            name="audiobook_search",
            description=("Search for audiobooks (AudiobookBay + Prowlarr) by title or "
                         "author. Returns top matches; use audiobook_get to download one."),
            tags=["audiobook", "books", "search", "audiobookbay", "abb", "find"],
            func=audiobook_search, provider="audiobook",
            example={"query": "Project Hail Mary"},
        ),
        Tool(
            name="audiobook_get",
            description=("Find and download an audiobook by title/author in one step "
                         "(picks the best match). Downloading is asynchronous."),
            tags=["audiobook", "books", "get", "acquire", "download", "audiobookbay", "abb"],
            func=audiobook_get, provider="audiobook",
            example={"query": "Project Hail Mary by Andy Weir"},
        ),
    ]
