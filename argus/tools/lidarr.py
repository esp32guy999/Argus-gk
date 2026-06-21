"""Lidarr acquisition lane — actually GET music into the library.

The OpenAPI *arr lane is read-only (status/list/lookup); it can find an artist in
metadata but not add them, which is why the agent could "find" Cyndi Lauper yet never
acquire her. This adds the write side as a smart native tool: lidarr_add_artist runs
the whole add flow deterministically in ONE call (lookup -> pick profiles + root
folder -> POST artist with a monitor+search addOptions), exposing just `artist` to the
model — robust where chaining raw OpenAPI calls on a weak model is not.

Side-effecting by design: the user asking for an artist IS the authorization (no
confirm gate). Reads Lidarr's base_url + key from the existing config/openapi.yaml
lidarr service (no duplicate secret). Acquisition is async in Lidarr — add+search
returns immediately; the download/import lands later and then shows in Navidrome.
"""
from __future__ import annotations

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

SERVICE = "lidarr"


def _cfg(manifest_path: str = "config/openapi.yaml") -> tuple[str, dict]:
    """(base_url, headers) for the lidarr service from the OpenAPI manifest."""
    data = yaml.safe_load(open(manifest_path)) or {}
    for svc in data.get("services", []):
        if svc.get("name") == SERVICE:
            return svc["base_url"].rstrip("/"), dict(svc.get("headers", {}))
    raise RuntimeError("no 'lidarr' service in config/openapi.yaml")


def has_config(manifest_path: str = "config/openapi.yaml") -> bool:
    try:
        _cfg(manifest_path)
        return True
    except Exception:
        return False


def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]:
    base, headers = _cfg(manifest_path)

    def _get(path, **params):
        r = httpx.get(f"{base}{path}", headers=headers, params=params or None, timeout=25)
        r.raise_for_status()
        return r.json()

    def _first_id(path, prefer=None):
        items = _get(path)
        if prefer:
            for it in items:
                if it.get("name", "").lower() == prefer.lower():
                    return it["id"]
        return items[0]["id"]

    def add_artist(artist: str) -> dict:
        """Add a music artist to Lidarr and start downloading their albums (the user
        will see them in their music library / Amperfy once downloaded). Use this to
        ACQUIRE music that isn't in the library yet. Acquisition runs in the
        background — report that it's been added and is downloading, not that it's
        already available."""
        try:
            results = _get("/api/v1/artist/lookup", term=artist)
        except httpx.HTTPError as e:
            raise ModelRetry(f"lidarr lookup failed: {e}")
        if not results:
            raise ModelRetry(f"lidarr: no artist found matching '{artist}'.")
        match = results[0]

        # Already in the library? lookup marks existing artists with a real id.
        if match.get("id"):
            return {"added": False, "already_in_library": True,
                    "artist": match.get("artistName"),
                    "note": "already in Lidarr; nothing to do"}

        try:
            quality = _first_id("/api/v1/qualityprofile", prefer="Any")
            metadata = _first_id("/api/v1/metadataprofile", prefer="Standard")
            root = _get("/api/v1/rootfolder")[0]["path"]
        except (httpx.HTTPError, IndexError, KeyError) as e:
            raise ModelRetry(f"lidarr: could not read profiles/root folder: {e}")

        body = {**match,
                "qualityProfileId": quality,
                "metadataProfileId": metadata,
                "rootFolderPath": root,
                "monitored": True,
                "addOptions": {"monitor": "all", "searchForMissingAlbums": True}}
        try:
            r = httpx.post(f"{base}/api/v1/artist", headers=headers, json=body, timeout=30)
        except httpx.RequestError as e:
            raise ModelRetry(f"lidarr add failed: {e}")
        if r.status_code >= 400:
            # Lidarr returns 400 with a clear message (e.g. already added)
            detail = r.text[:200]
            if "already been added" in detail.lower():
                return {"added": False, "already_in_library": True,
                        "artist": match.get("artistName")}
            raise ModelRetry(f"lidarr add returned HTTP {r.status_code}: {detail}")
        return {"added": True, "artist": match.get("artistName"),
                "searching": True,
                "note": "added + searching; albums will download and appear once imported"}

    return [
        Tool(
            name="lidarr_add_artist",
            description=("Add a music artist to Lidarr and start downloading their albums. "
                         "Use to ACQUIRE music not yet in the library. Downloading is "
                         "asynchronous — it won't be playable immediately."),
            tags=["music", "lidarr", "add", "acquire", "download", "get", "artist"],
            func=add_artist,
            provider="lidarr",
            example={"artist": "Cyndi Lauper"},
        )
    ]
