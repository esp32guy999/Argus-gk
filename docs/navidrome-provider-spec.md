# Navidrome / Subsonic Provider — build spec

Implement **`argus/tools/navidrome.py`**: a small Subsonic-API client exposing music
tools (playlists + a genre "DJ"). **MIRROR the structure of `argus/tools/web.py`**
(module-level helpers + `tools()` returning `registry.Tool`s; raise `ModelRetry` on
failure to teach the model).

## Hard requirement
Make `tests/test_navidrome_provider.py` pass. Create ONLY `argus/tools/navidrome.py`.

## Public interface (exactly this)
```python
def tools(manifest_path: str = "config/navidrome.yaml") -> list[Tool]: ...
```
`Tool` is `from ..registry import Tool`. Load the manifest with `yaml.safe_load`:
`base_url`, `username`, `password`, `default_count` (int, default 30).

## Subsonic auth (CRITICAL — every request)
Subsonic uses salt+token auth, NOT a header. For each request build query params:
- `u` = username
- `s` = a fresh random salt (hex string, e.g. `secrets.token_hex(8)`)
- `t` = `hashlib.md5((password + salt).encode()).hexdigest()`
- `v` = `"1.16.1"`, `c` = `"argus"`, `f` = `"json"`

Request URL: `f"{base_url}/rest/{endpoint}.view"` with those params (+ endpoint params).
Use `httpx.get(url, params=..., timeout=20)`. Parse `resp.json()["subsonic-response"]`;
if its `status` != `"ok"`, raise `ModelRetry` with the error message. Network error ->
`ModelRetry`. A NEW salt per request.

## Tools (4)
1. **navidrome_list_playlists()** — GET `getPlaylists` → return
   `[{"id","name","songCount"}, ...]` from `resp["playlists"]["playlist"]`
   (it may be missing/!list → treat as []).
2. **navidrome_list_genres()** — GET `getGenres` → return a list of genre name
   strings from `resp["genres"]["genre"][*]["value"]`.
3. **navidrome_dj(genre: str, count: int = <default_count>)** — the DJ. Steps:
   - GET `getRandomSongs` with params `genre=genre`, `size=count` → songs from
     `resp["randomSongs"]["song"]`. If empty → `ModelRetry(f"no songs for genre {genre}")`.
   - Target playlist name = `f"🎧 {genre}"`. Look it up via `getPlaylists`; if a
     playlist with that exact name exists, GET `deletePlaylist` with `id=<its id>`
     (refresh = replace).
   - GET `createPlaylist` with `name=<target>` and one `songId` param per song id.
     (httpx: `params=[("name", target), ("songId", id1), ("songId", id2), ...]` — a
     list of tuples sends repeated keys.)
   - Return `{"playlist": target, "count": len(songs),
     "tracks": [{"title","artist"}, ...]}`.
4. **navidrome_create_playlist(name: str, query: str)** — GET `search3` with
   `query=query` → songs from `resp["searchResult3"]["song"]`. If empty →
   `ModelRetry`. GET `createPlaylist` with `name` + a `songId` per found id.
   Return `{"playlist": name, "count": <n songs>}`.

All four `Tool`s: `provider="navidrome"`, sensible `tags` (music/playlist/dj/genre),
and an `example`. Tool `name` = the function name (e.g. `"navidrome_dj"`).

## Notes
- Helper suggestion: `_call(cfg, endpoint, **params) -> dict` that builds auth +
  params, does the GET, validates `subsonic-response.status`, returns the response dict.
- Allowed deps: `httpx`, `yaml`, `hashlib`, `secrets`. ~110-140 lines.
- Subsonic returns a single dict (not a list) when there's one item in some fields;
  the test always returns lists, but guard `.get(..., [])` and tolerate non-lists.
