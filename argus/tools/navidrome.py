"""Navidrome / Subsonic provider — music tools: playlists + a genre "DJ".

- navidrome_list_playlists(): list playlists (id, name, songCount).
- navidrome_list_genres(): list available genres.
- navidrome_dj(genre, count): build a playlist of random songs in a genre, refreshing
  it if one with that name already exists.
- navidrome_create_playlist(name, query): search, then create a playlist from results.

Subsonic salt+token auth (md5(password+salt), fresh salt per request); the plaintext
password stays in the gitignored manifest and never goes on the wire. Errors ->
ModelRetry. (80B-drafted, Claude-reviewed: fixed config binding + provider.)
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool


def _call(cfg: dict, endpoint: str, **params: Any) -> dict:
    """One authenticated Subsonic request. Returns the subsonic-response dict."""
    salt = secrets.token_hex(8)
    token = hashlib.md5((str(cfg["password"]) + salt).encode()).hexdigest()
    query = [
        ("u", cfg["username"]), ("s", salt), ("t", token),
        ("v", "1.16.1"), ("c", "argus"), ("f", "json"),
    ]
    for k, v in params.items():
        if isinstance(v, (list, tuple)):
            query.extend((k, item) for item in v)
        else:
            query.append((k, v))
    url = f"{str(cfg['base_url']).rstrip('/')}/rest/{endpoint}.view"
    try:
        resp = httpx.get(url, params=query, timeout=20)
    except httpx.RequestError as e:
        raise ModelRetry(f"navidrome request failed: {e}")
    try:
        body = resp.json()["subsonic-response"]
    except Exception:
        raise ModelRetry(f"navidrome returned an unexpected response (HTTP {resp.status_code}).")
    if body.get("status") != "ok":
        msg = (body.get("error") or {}).get("message", "unknown error")
        raise ModelRetry(f"navidrome API error: {msg}")
    return body


def _aslist(value) -> list:
    """Subsonic returns a single dict for 1-item collections; normalize to a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tools(manifest_path: str = "config/navidrome.yaml") -> list[Tool]:
    """Provider entry point. Loads the manifest once and binds it into each tool."""
    with open(manifest_path) as f:
        cfg = yaml.safe_load(f)
    default_count = int(cfg.get("default_count", 30))

    def list_playlists() -> list:
        """List all playlists in the music library (id, name, song count)."""
        body = _call(cfg, "getPlaylists")
        pls = _aslist((body.get("playlists") or {}).get("playlist"))
        return [{"id": p.get("id"), "name": p.get("name"),
                 "songCount": p.get("songCount")} for p in pls]

    def list_genres() -> list:
        """List the music genres available in the library."""
        body = _call(cfg, "getGenres")
        gs = _aslist((body.get("genres") or {}).get("genre"))
        return [g["value"] for g in gs if isinstance(g, dict) and "value" in g]

    def dj(genre: str, count: int = default_count) -> dict:
        """Be a DJ: build a playlist of random songs from a genre. Names it '🎧 <genre>'
        and refreshes it if it already exists. The user plays it in their Amperfy app."""
        body = _call(cfg, "getRandomSongs", genre=genre, size=count)
        songs = _aslist((body.get("randomSongs") or {}).get("song"))
        if not songs:
            raise ModelRetry(f"navidrome: no songs found for genre '{genre}'. "
                             "Call navidrome_list_genres to see valid genres.")
        target = f"🎧 {genre}"
        existing = next((p for p in list_playlists() if p["name"] == target), None)
        if existing:
            _call(cfg, "deletePlaylist", id=existing["id"])
        _call(cfg, "createPlaylist", name=target, songId=[s["id"] for s in songs])
        return {"playlist": target, "count": len(songs),
                "tracks": [{"title": s.get("title"), "artist": s.get("artist")} for s in songs]}

    def create_playlist(name: str, query: str) -> dict:
        """Create a playlist named `name` from songs matching a search `query`."""
        body = _call(cfg, "search3", query=query)
        songs = _aslist((body.get("searchResult3") or {}).get("song"))
        if not songs:
            raise ModelRetry(f"navidrome: no songs matched '{query}'.")
        _call(cfg, "createPlaylist", name=name, songId=[s["id"] for s in songs])
        return {"playlist": name, "count": len(songs)}

    return [
        Tool(name="navidrome_list_playlists",
             description="List all playlists in the Navidrome music library.",
             tags=["music", "playlist", "list", "navidrome"],
             func=list_playlists, provider="navidrome", example={}),
        Tool(name="navidrome_list_genres",
             description="List the music genres available in the Navidrome library.",
             tags=["music", "genre", "list", "navidrome"],
             func=list_genres, provider="navidrome", example={}),
        Tool(name="navidrome_dj",
             description=("Be a DJ: build (and refresh) a playlist of random songs from a "
                          "genre, named '🎧 <genre>'. The user plays it in Amperfy."),
             tags=["music", "dj", "genre", "playlist", "play", "navidrome"],
             func=dj, provider="navidrome", example={"genre": "Jazz", "count": 30}),
        Tool(name="navidrome_create_playlist",
             description="Create a playlist from a search query (e.g. an artist or song).",
             tags=["music", "playlist", "create", "search", "navidrome"],
             func=create_playlist, provider="navidrome", example={"name": "Roadtrip", "query": "Tom Petty"}),
    ]
