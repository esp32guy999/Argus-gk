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
import re
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
                   search_cmd=("MoviesSearch", "movieIds", True),
                   tags=["movie", "radarr", "add", "acquire", "download", "get", "film"],
                   desc="Add a movie to Radarr and start downloading it.",
                   example={"movie": "The Matrix"}),
    "sonarr": dict(api="v3", noun="series", param="series", tool="sonarr_add_series",
                   lookup="/series/lookup", add="/series", qp_prefer="HD-1080p",
                   metadata=False, monitor_values=_SERIES_MON, monitor_default="all",
                   search_opt="searchForMissingEpisodes", extra=None,
                   search_cmd=("SeriesSearch", "seriesId", False),
                   tags=["tv", "sonarr", "add", "acquire", "download", "get", "series", "show"],
                   desc="Add a TV series to Sonarr and start downloading it.",
                   example={"series": "Severance", "monitor": "latestSeason"}),
    "lidarr": dict(api="v1", noun="artist", param="artist", tool="lidarr_add_artist",
                   lookup="/artist/lookup", add="/artist", qp_prefer="Any",
                   metadata=True, monitor_values=_MUSIC_MON, monitor_default="all",
                   search_opt="searchForMissingAlbums", extra=None,
                   search_cmd=("ArtistSearch", "artistId", False),
                   tags=["music", "lidarr", "add", "acquire", "download", "get", "artist"],
                   desc="Add a music artist to Lidarr and start downloading their albums.",
                   example={"artist": "Cyndi Lauper", "monitor": "latest"}),
    "readarr": dict(api="v1", noun="author", param="author", tool="readarr_add_author",
                    lookup="/author/lookup", add="/author", qp_prefer="Spoken",
                    metadata=True, monitor_values=_MUSIC_MON, monitor_default="all",
                    search_opt="searchForMissingBooks", extra=None,
                    search_cmd=("AuthorSearch", "authorId", False),
                    tags=["audiobook", "readarr", "bookshelf", "add", "acquire", "download", "get", "author"],
                    desc="Add an author to Readarr/Bookshelf and download their audiobooks "
                         "(Spoken profile; imports to the Audiobookshelf library).",
                    example={"author": "Brandon Sanderson"}),
}


# Child-item monitoring for the already-in-library path. A re-request must MONITOR the
# items it's missing (the *arr apps only search MONITORED items), else the search runs
# over nothing and "downloads" never start. radarr omitted: a movie is its own item.
CHILDREN = {
    "lidarr":  dict(list_path="/album",   id_param="artistId", monitor_path="/album/monitor",  id_key="albumIds"),
    "readarr": dict(list_path="/book",    id_param="authorId", monitor_path="/book/monitor",   id_key="bookIds"),
    "sonarr":  dict(list_path="/episode", id_param="seriesId", monitor_path="/episode/monitor", id_key="episodeIds"),
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

    def _present(it) -> bool:
        """True if this child item's files are already fully present."""
        st = it.get("statistics") or {}
        fc = st.get("trackFileCount", st.get("bookFileCount", st.get("fileCount")))
        tc = st.get("trackCount", st.get("bookCount"))
        if isinstance(fc, int) and isinstance(tc, int):
            return tc > 0 and fc >= tc
        return bool(it.get("hasFile"))

    def _remonitor(match: dict, monitor: str) -> dict:
        """Already-in-library path. The old code fired a search WITHOUT monitoring, so
        the *arr searched its (zero) monitored items and downloaded nothing — while
        returning searching:true (hollow success). Now: ensure the entity + its missing
        items are monitored, THEN search, and report honestly (incl. 'nothing missing')."""
        if not match.get("monitored"):        # entity must be monitored to search its items
            try:
                httpx.put(f"{base}/api/{api}{spec['add']}/{match['id']}",
                          headers=headers, json={**match, "monitored": True}, timeout=30)
            except httpx.RequestError:
                pass
        child = CHILDREN.get(spec.get("svc"))
        monitored_now = missing = 0
        if child:
            try:
                items = _get(child["list_path"], **{child["id_param"]: match["id"]})
            except httpx.HTTPError:
                items = []
            missing = sum(1 for it in items if not _present(it))
            if monitor == "none":
                targets = []
            elif monitor in ("all", "future", "existing"):
                targets = items
            else:                              # missing / latest / first -> what we lack
                targets = [it for it in items if not _present(it)]
            ids = [it["id"] for it in targets]
            if ids:
                try:
                    r = httpx.put(f"{base}/api/{api}{child['monitor_path']}", headers=headers,
                                  json={child["id_key"]: ids, "monitored": True}, timeout=30)
                    monitored_now = len(ids) if r.status_code < 400 else 0
                except httpx.RequestError:
                    monitored_now = 0
        else:                                  # radarr: the movie IS the item
            missing = 0 if match.get("hasFile") else 1
            monitored_now = 0 if match.get("hasFile") else 1
        # honest early-outs — no false "searching"
        if monitor == "none":
            return {"added": False, "already_in_library": True, "title": _title(match),
                    "searching": False, "monitored": 0,
                    "note": "already in the library; monitor=none, so nothing was monitored or searched"}
        if missing == 0:
            return {"added": False, "already_in_library": True, "title": _title(match),
                    "searching": False, "monitored": monitored_now,
                    "note": "already in the library and every item is already present — nothing to download"}
        cmd_name, key, is_list = spec["search_cmd"]
        payload = {"name": cmd_name, key: [match["id"]] if is_list else match["id"]}
        try:
            cr = httpx.post(f"{base}/api/{api}/command", headers=headers, json=payload, timeout=30)
            searched = cr.status_code < 400
        except httpx.RequestError:
            searched = False
        return {"added": False, "already_in_library": True, "title": _title(match),
                "monitored": monitored_now, "missing": missing, "searching": searched,
                "note": (f"already in library; monitored {monitored_now} missing item(s) and "
                         "started a search — will download and import once grabbed"
                         if searched else
                         f"monitored {monitored_now} item(s) but the search could not be started")}

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
            return _remonitor(match, monitor)
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


def _norm(s: str) -> str:
    """Lowercase + collapse punctuation to spaces, so 'She's So Unusual' matches
    'shes so unusual' / 'she s so unusual'."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _album_score(want: str, title: str) -> int:
    """How well a requested album name matches a real album title. 3=exact,
    2=substring either way, 1=majority token overlap (so 'she's so strange' still
    lands on 'She's So Unusual'), 0=no match."""
    w, t = _norm(want), _norm(title)
    if not w or not t:
        return 0
    if w == t:
        return 3
    if w in t or t in w:
        return 2
    wt, tt = set(w.split()), set(t.split())
    common = wt & tt
    return 1 if common and len(common) >= max(1, len(wt) // 2) else 0


def _make_lidarr_album(base: str, headers: dict):
    """The granular music path: target ONE album by an artist (album search is the
    floor that artist-add can't hit). Finds/adds the artist, monitors the matched
    album, and fires an AlbumSearch for just it."""
    api = "v1"

    def _get(path, **params):
        r = httpx.get(f"{base}/api/{api}{path}", headers=headers, params=params or None, timeout=35)
        r.raise_for_status()
        return r.json()

    def _send(method, path, body):
        r = httpx.request(method, f"{base}/api/{api}{path}", headers=headers, json=body, timeout=35)
        if r.status_code >= 400:
            raise ModelRetry(f"lidarr_get_album: {method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    def _quality_id(quality):
        items = _get("/qualityprofile")
        want = (quality or "Any").lower()
        for it in items:
            if it.get("name", "").lower() == want:
                return it["id"]
        if quality:
            raise ModelRetry(f"lidarr_get_album: quality '{quality}' not found. "
                             f"Options: {[it['name'] for it in items]}.")
        return items[0]["id"]

    def get_album(artist: str, album: str, quality: Optional[str] = None) -> dict:
        try:
            lib = _get("/artist")
        except httpx.HTTPError as e:
            raise ModelRetry(f"lidarr_get_album: could not list artists: {e}")
        wa = _norm(artist)
        match = (next((a for a in lib if _norm(a.get("artistName")) == wa), None)
                 or next((a for a in lib if wa and wa in _norm(a.get("artistName"))), None))

        if not match:   # not in library yet — add it WITHOUT grabbing the whole catalogue
            results = _get("/artist/lookup", term=artist)
            if not results:
                raise ModelRetry(f"lidarr_get_album: no artist found matching '{artist}'.")
            cand = results[0]
            mitems = _get("/metadataprofile")
            body = {**cand, "qualityProfileId": _quality_id(quality),
                    "metadataProfileId": next((m["id"] for m in mitems if m.get("name") == "Standard"),
                                              mitems[0]["id"]),
                    "rootFolderPath": _get("/rootfolder")[0]["path"], "monitored": True,
                    "addOptions": {"monitor": "none", "searchForMissingAlbums": False}}
            match = _send("POST", "/artist", body)

        # An existing artist may be monitored=false; ensure it's monitored so the album
        # search isn't silently ignored (the already-in-library lesson). Best-effort.
        if not match.get("monitored"):
            try:
                _send("PUT", f"/artist/{match['id']}", {**match, "monitored": True})
            except ModelRetry:
                pass

        albums = _get("/album", artistId=match["id"])
        if not albums:
            return {"artist": match.get("artistName"), "added_artist": True, "searching": False,
                    "note": "artist added; album metadata is still populating — ask for the album again in a few seconds"}

        best = max(albums, key=lambda al: _album_score(album, al.get("title", "")))
        if _album_score(album, best.get("title", "")) == 0:
            raise ModelRetry(f"lidarr_get_album: '{album}' didn't match any album by "
                             f"{match.get('artistName')}. Albums: {[a.get('title') for a in albums]}.")

        if not best.get("monitored"):
            _send("PUT", "/album/monitor", {"albumIds": [best["id"]], "monitored": True})
        cmd = _send("POST", "/command", {"name": "AlbumSearch", "albumIds": [best["id"]]})
        st = best.get("statistics") or {}
        return {"artist": match.get("artistName"), "album": best.get("title"),
                "monitored": True, "searching": True,
                "have_tracks": st.get("trackFileCount", 0), "total_tracks": st.get("trackCount", 0),
                "command_id": cmd.get("id"),
                "note": "monitored + searching this album; it imports once a release is grabbed"}

    get_album.__name__ = "lidarr_get_album"
    get_album.__doc__ = (
        "Acquire ONE specific album by a music artist (not the whole discography). "
        "Finds or adds the artist, monitors the matching album, and starts a targeted "
        "search. Use this when the user names an album. Album matching is fuzzy. "
        "Downloading is asynchronous — report it's downloading, not available. "
        "Optional quality = a quality-profile name.")
    return get_album


def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]:
    out = []
    for name, (base, headers) in _services(manifest_path).items():
        spec = dict(SPECS[name], svc=name)
        out.append(Tool(
            name=spec["tool"], description=spec["desc"] + " (acquires + starts download)",
            tags=spec["tags"], func=_make_add(base, headers, spec),
            provider=name, example=spec["example"],
        ))
        if name == "lidarr":   # album-level granularity on top of artist-level add
            out.append(Tool(
                name="lidarr_get_album",
                description="Acquire ONE specific album by an artist (album-level, not the whole discography).",
                tags=["music", "lidarr", "album", "acquire", "download", "get", "song", "track"],
                func=_make_lidarr_album(base, headers),
                provider="lidarr",
                example={"artist": "Cyndi Lauper", "album": "She's So Unusual"},
            ))
    return out
