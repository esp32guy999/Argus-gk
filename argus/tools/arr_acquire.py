"""*arr acquisition lane — actually GET media into the library (radarr/sonarr/lidarr).

The OpenAPI *arr lane is read-only (status/list/lookup); it can find a title in
metadata but not acquire it. This adds the write side as smart native tools, one per
service, that run the whole add flow in ONE deterministic call: lookup -> pick quality
(+ metadata) profile + root folder -> POST with addOptions {monitor, search}. Each
tool exposes a single param to the model — chaining raw OpenAPI calls to assemble these
bodies is too fragile on the local model.

The three services differ only in: API version, the addOptions search key, and a
couple of extra body fields (lidarr metadataProfileId; radarr minimumAvailability) —
captured in SPECS, so the logic lives once.

Side-effecting with NO confirm gate: the user asking IS the authorization. Creds come
from the existing config/openapi.yaml service entries (no duplicate secrets). Acquisition
is asynchronous in *arr — add+search returns immediately; the download/import lands later.
"""
from __future__ import annotations

import inspect

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

# Per-service knobs; everything else is shared.
SPECS = {
    "radarr": dict(api="v3", noun="movie", param="movie", tool="radarr_add_movie",
                   lookup="/movie/lookup", add="/movie", qp_prefer="HD-1080p",
                   metadata=False, monitor=None, search_opt="searchForMovie",
                   extra={"minimumAvailability": "released"},
                   tags=["movie", "radarr", "add", "acquire", "download", "get", "film"],
                   desc="Add a movie to Radarr and start downloading it.",
                   example={"movie": "The Matrix"}),
    "sonarr": dict(api="v3", noun="series", param="series", tool="sonarr_add_series",
                   lookup="/series/lookup", add="/series", qp_prefer="HD-1080p",
                   metadata=False, monitor="all", search_opt="searchForMissingEpisodes",
                   extra=None,
                   tags=["tv", "sonarr", "add", "acquire", "download", "get", "series", "show"],
                   desc="Add a TV series to Sonarr and start downloading it.",
                   example={"series": "Breaking Bad"}),
    "lidarr": dict(api="v1", noun="artist", param="artist", tool="lidarr_add_artist",
                   lookup="/artist/lookup", add="/artist", qp_prefer="Any",
                   metadata=True, monitor="all", search_opt="searchForMissingAlbums",
                   extra=None,
                   tags=["music", "lidarr", "add", "acquire", "download", "get", "artist"],
                   desc="Add a music artist to Lidarr and start downloading their albums.",
                   example={"artist": "Cyndi Lauper"}),
}


def _services(manifest_path: str) -> dict:
    """{service_name: (base_url, headers)} for the SPEC'd services present in the manifest."""
    data = yaml.safe_load(open(manifest_path)) or {}
    out = {}
    for svc in data.get("services", []):
        name = svc.get("name")
        if name in SPECS:
            out[name] = (svc["base_url"].rstrip("/"), dict(svc.get("headers", {})))
    return out


def has_any(manifest_path: str = "config/openapi.yaml") -> bool:
    try:
        return bool(_services(manifest_path))
    except Exception:
        return False


def _make_add(base: str, headers: dict, spec: dict):
    api = spec["api"]

    def _get(path, **params):
        r = httpx.get(f"{base}/api/{api}{path}", headers=headers,
                      params=params or None, timeout=25)
        r.raise_for_status()
        return r.json()

    def _profile_id(path, prefer):
        items = _get(path)
        for it in items:
            if it.get("name", "").lower() == prefer.lower():
                return it["id"]
        return items[0]["id"]

    def add(**kwargs):
        query = kwargs[spec["param"]]
        noun = spec["noun"]
        try:
            results = _get(spec["lookup"], term=query)
        except httpx.HTTPError as e:
            raise ModelRetry(f"{spec['tool']}: lookup failed: {e}")
        if not results:
            raise ModelRetry(f"{spec['tool']}: no {noun} found matching '{query}'.")
        match = results[0]
        if match.get("id"):   # lookup marks already-in-library items with a real id
            return {"added": False, "already_in_library": True,
                    "title": match.get("title") or match.get("artistName"),
                    "note": f"already in the library; nothing to do"}
        try:
            qp = _profile_id("/qualityprofile", spec["qp_prefer"])
            root = _get("/rootfolder")[0]["path"]
            mp = _profile_id("/metadataprofile", "Standard") if spec["metadata"] else None
        except (httpx.HTTPError, IndexError, KeyError) as e:
            raise ModelRetry(f"{spec['tool']}: could not read profiles/root folder: {e}")

        body = {**match, "qualityProfileId": qp, "rootFolderPath": root, "monitored": True}
        if mp is not None:
            body["metadataProfileId"] = mp
        if spec["extra"]:
            body.update(spec["extra"])
        addopts = {spec["search_opt"]: True}
        if spec["monitor"]:
            addopts["monitor"] = spec["monitor"]
        body["addOptions"] = addopts

        try:
            r = httpx.post(f"{base}/api/{api}{spec['add']}", headers=headers, json=body, timeout=30)
        except httpx.RequestError as e:
            raise ModelRetry(f"{spec['tool']}: add failed: {e}")
        if r.status_code >= 400:
            detail = r.text[:200]
            if "already been added" in detail.lower():
                return {"added": False, "already_in_library": True,
                        "title": match.get("title") or match.get("artistName")}
            raise ModelRetry(f"{spec['tool']}: add returned HTTP {r.status_code}: {detail}")
        return {"added": True, "title": match.get("title") or match.get("artistName"),
                "searching": True,
                "note": f"added + searching; will download and appear once imported"}

    # Give the tool a meaningful single param name (movie/series/artist) for the model.
    add.__name__ = spec["tool"]
    add.__doc__ = (spec["desc"] + " Use to ACQUIRE media not yet in the library. "
                   "Downloading is asynchronous — report it's downloading, not available.")
    add.__signature__ = inspect.Signature(
        [inspect.Parameter(spec["param"], inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)])
    add.__annotations__ = {spec["param"]: str, "return": dict}  # pydantic-ai reads these
    return add


def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]:
    out = []
    for name, (base, headers) in _services(manifest_path).items():
        spec = SPECS[name]
        out.append(Tool(
            name=spec["tool"], description=spec["desc"] + " (acquires + starts download)",
            tags=spec["tags"], func=_make_add(base, headers, spec),
            provider=name, example=spec["example"],
        ))
    return out
