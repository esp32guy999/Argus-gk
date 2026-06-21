"""*arr acquisition lane — actually GET media into the library (radarr/sonarr/lidarr/readarr).

The OpenAPI *arr lane is read-only (status/list/lookup); it can find a title in
metadata but not acquire it. This adds the write side as smart native tools, one per
service, that run the whole add flow in ONE deterministic call: lookup -> pick quality
(+ metadata) profile + root folder -> POST with addOptions {monitor, search}. Each
tool exposes a meaningful primary param plus optional REFINEMENTS:
  - monitor: which items to grab (e.g. sonarr 'latestSeason', lidarr 'latest') so you
    can say "just the newest season" instead of the whole back catalogue.
  - quality: a quality-profile name (e.g. 'Lossless', 'HD-1080p', 'eBook').

Per-service differences (API version, search-option key, monitor vocabulary, extra
body fields: lidarr/readarr metadataProfileId, radarr minimumAvailability) live in
SPECS, so the logic exists once.

Side-effecting with NO confirm gate: the user asking IS the authorization. Creds come
from the existing config/openapi.yaml service entries. Acquisition is asynchronous —
add+search returns immediately; the download/import lands later.
"""
from __future__ import annotations

import inspect
from typing import Optional

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

# Monitor vocabularies (addOptions.monitor) per *arr family.
_SERIES_MON = ["all", "future", "missing", "existing", "firstSeason", "latestSeason", "pilot", "none"]
_MUSIC_MON = ["all", "future", "missing", "existing", "latest", "first", "none"]

SPECS = {
    "radarr": dict(api="v3", noun="movie", param="movie", tool="radarr_add_movie",
                   lookup="/movie/lookup", add="/movie", qp_prefer="HD-1080p",
                   metadata=False, monitor_values=None, monitor_default=None,
                   search_opt="searchForMovie", extra={"minimumAvailability": "released"},
                   tags=["movie", "radarr", "add", "acquire", "download", "get", "film"],
                   desc="Add a movie to Radarr and start downloading it.",
                   example={"movie": "The Matrix"}),
    "sonarr": dict(api="v3", noun="series", param="series", tool="sonarr_add_series",
                   lookup="/series/lookup", add="/series", qp_prefer="HD-1080p",
                   metadata=False, monitor_values=_SERIES_MON, monitor_default="all",
                   search_opt="searchForMissingEpisodes", extra=None,
                   tags=["tv", "sonarr", "add", "acquire", "download", "get", "series", "show"],
                   desc="Add a TV series to Sonarr and start downloading it.",
                   example={"series": "Severance", "monitor": "latestSeason"}),
    "lidarr": dict(api="v1", noun="artist", param="artist", tool="lidarr_add_artist",
                   lookup="/artist/lookup", add="/artist", qp_prefer="Any",
                   metadata=True, monitor_values=_MUSIC_MON, monitor_default="all",
                   search_opt="searchForMissingAlbums", extra=None,
                   tags=["music", "lidarr", "add", "acquire", "download", "get", "artist"],
                   desc="Add a music artist to Lidarr and start downloading their albums.",
                   example={"artist": "Cyndi Lauper", "monitor": "latest"}),
    "readarr": dict(api="v1", noun="author", param="author", tool="readarr_add_author",
                    lookup="/author/lookup", add="/author", qp_prefer="eBook",
                    metadata=True, monitor_values=_MUSIC_MON, monitor_default="all",
                    search_opt="searchForMissingBooks", extra=None,
                    tags=["book", "readarr", "add", "acquire", "download", "get", "author", "ebook"],
                    desc="Add an author to Readarr and start downloading their books.",
                    example={"author": "Brandon Sanderson"}),
}


def _services(manifest_path: str) -> dict:
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


def _title(match: dict) -> str:
    return match.get("title") or match.get("artistName") or match.get("authorName") or "?"


def _make_add(base: str, headers: dict, spec: dict):
    api = spec["api"]

    def _get(path, **params):
        r = httpx.get(f"{base}/api/{api}{path}", headers=headers,
                      params=params or None, timeout=35)   # readarr lookups are slow (~10s)
        r.raise_for_status()
        return r.json()

    def _quality_id(quality: Optional[str]):
        items = _get("/qualityprofile")
        want = (quality or spec["qp_prefer"]).lower()
        for it in items:
            if it.get("name", "").lower() == want:
                return it["id"]
        if quality:   # explicit but not found -> teach with the valid options
            raise ModelRetry(f"{spec['tool']}: quality '{quality}' not found. "
                             f"Options: {[it['name'] for it in items]}.")
        return items[0]["id"]

    def add(**kwargs):
        query = kwargs[spec["param"]]
        monitor = kwargs.get("monitor") or spec["monitor_default"]
        quality = kwargs.get("quality")
        noun = spec["noun"]

        if spec["monitor_values"] and monitor not in spec["monitor_values"]:
            raise ModelRetry(f"{spec['tool']}: monitor '{monitor}' invalid. "
                             f"Options: {spec['monitor_values']}.")
        try:
            results = _get(spec["lookup"], term=query)
        except httpx.HTTPError as e:
            raise ModelRetry(f"{spec['tool']}: lookup failed: {e}")
        if not results:
            raise ModelRetry(f"{spec['tool']}: no {noun} found matching '{query}'.")
        match = results[0]
        if match.get("id"):
            return {"added": False, "already_in_library": True, "title": _title(match),
                    "note": "already in the library; nothing to do"}
        try:
            qp = _quality_id(quality)
            root = _get("/rootfolder")[0]["path"]
            mp = None
            if spec["metadata"]:
                mitems = _get("/metadataprofile")
                mp = next((m["id"] for m in mitems if m.get("name") == "Standard"), mitems[0]["id"])
        except (httpx.HTTPError, IndexError, KeyError) as e:
            raise ModelRetry(f"{spec['tool']}: could not read profiles/root folder: {e}")

        body = {**match, "qualityProfileId": qp, "rootFolderPath": root, "monitored": True}
        if spec["metadata"]:
            body["metadataProfileId"] = mp
        if spec["extra"]:
            body.update(spec["extra"])
        addopts = {spec["search_opt"]: True}
        if spec["monitor_values"]:
            addopts["monitor"] = monitor
        body["addOptions"] = addopts

        try:
            r = httpx.post(f"{base}/api/{api}{spec['add']}", headers=headers, json=body, timeout=30)
        except httpx.RequestError as e:
            raise ModelRetry(f"{spec['tool']}: add failed: {e}")
        if r.status_code >= 400:
            detail = r.text[:200]
            if "already been added" in detail.lower():
                return {"added": False, "already_in_library": True, "title": _title(match)}
            raise ModelRetry(f"{spec['tool']}: add returned HTTP {r.status_code}: {detail}")
        result = {"added": True, "title": _title(match), "searching": True,
                  "note": "added + searching; will download and appear once imported"}
        if spec["monitor_values"]:
            result["monitor"] = monitor
        return result

    # Build a clean signature: primary param (required) + optional monitor/quality.
    params = [inspect.Parameter(spec["param"], inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)]
    ann = {spec["param"]: str, "return": dict}
    if spec["monitor_values"]:
        params.append(inspect.Parameter("monitor", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        default=spec["monitor_default"], annotation=Optional[str]))
        ann["monitor"] = Optional[str]
    params.append(inspect.Parameter("quality", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                    default=None, annotation=Optional[str]))
    ann["quality"] = Optional[str]
    add.__name__ = spec["tool"]
    mon_doc = (f" Optional monitor (which items to grab): {spec['monitor_values']}."
               if spec["monitor_values"] else "")
    add.__doc__ = (spec["desc"] + " Use to ACQUIRE media not yet in the library. Downloading "
                   "is asynchronous — report it's downloading, not available." + mon_doc +
                   " Optional quality = a quality-profile name.")
    add.__signature__ = inspect.Signature(params)
    add.__annotations__ = ann
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
