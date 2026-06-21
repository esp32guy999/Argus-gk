"""Contract test for the *arr acquisition lane (argus/tools/arr_acquire.py).

One fake *arr server (path-routed) validates the add flow for all three services:
lookup -> profiles + rootfolder -> POST with the right body + addOptions, plus
already-in-library and no-match handling. Offline. python tests/test_arr_acquire.py
"""
from __future__ import annotations
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POSTS = {}   # api_path -> last posted body


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _j(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path); p = u.path
        term = parse_qs(u.query).get("term", [""])[0].lower()
        if p.endswith("/lookup"):
            if "nothing" in term:
                self._j(200, [])
            elif "existing" in term:
                self._j(200, [{"title": "Existing", "artistName": "Existing", "id": 5}])
            else:
                self._j(200, [{"title": "Found", "artistName": "Found",
                               "tmdbId": 1, "tvdbId": 2, "foreignArtistId": "x"}])
        elif p.endswith("/qualityprofile"):
            self._j(200, [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"},
                          {"id": 7, "name": "Lossless"}, {"id": 9, "name": "eBook"}])
        elif p.endswith("/metadataprofile"):
            self._j(200, [{"id": 1, "name": "Standard"}])
        elif p.endswith("/rootfolder"):
            self._j(200, [{"path": "/media"}])
        else:
            self._j(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode() or "{}")
        POSTS[urlparse(self.path).path] = body
        self._j(201, {**body, "id": 99})


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import arr_acquire

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    fd, mpath = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, ("services:\n"
                  f"  - name: radarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n"
                  f"  - name: sonarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n"
                  f"  - name: lidarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n"
                  f"  - name: readarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n").encode())
    os.close(fd)
    try:
        by = {t.name: t for t in arr_acquire.tools(mpath)}
        assert set(by) == {"radarr_add_movie", "sonarr_add_series",
                           "lidarr_add_artist", "readarr_add_author"}, list(by)
        print("PASS: all four add tools registered (incl. readarr)")

        # param names come through to the schema (movie/series/artist/author)
        for tool in by.values():
            tool.as_pydantic_tool()  # builds without error
        print("PASS: tools build (per-service param names + optional refinements)")

        # REFINABLE: monitor preset routes into addOptions; quality picks the profile
        POSTS.clear()
        out = by["sonarr_add_series"].func(series="Severance", monitor="latestSeason", quality="HD-1080p")
        b = POSTS["/api/v3/series"]
        assert b["addOptions"]["monitor"] == "latestSeason", b
        assert b["qualityProfileId"] == 4 and out["monitor"] == "latestSeason", (b, out)
        print("PASS: refinable monitor + quality (sonarr latestSeason / HD-1080p)")

        # invalid monitor -> teaching ModelRetry with options
        try:
            by["lidarr_add_artist"].func(artist="X", monitor="bogus")
            print("FAIL: expected ModelRetry on bad monitor"); return 1
        except ModelRetry as e:
            assert "invalid" in str(e), str(e)
        # invalid quality -> teaching ModelRetry with options
        try:
            by["lidarr_add_artist"].func(artist="X", quality="nope")
            print("FAIL: expected ModelRetry on bad quality"); return 1
        except ModelRetry as e:
            assert "not found" in str(e), str(e)
        print("PASS: invalid monitor/quality -> teaching ModelRetry")

        # readarr: author noun, v1, metadata profile, searchForMissingBooks
        POSTS.clear()
        out = by["readarr_add_author"].func(author="Brandon Sanderson", quality="eBook")
        b = POSTS["/api/v1/author"]
        assert b["metadataProfileId"] == 1 and b["qualityProfileId"] == 9, b
        assert b["addOptions"]["searchForMissingBooks"] is True, b
        print("PASS: readarr_add_author body (eBook quality, metadata, searchForMissingBooks)")

        # radarr: HD-1080p profile, /media root, minimumAvailability, searchForMovie
        POSTS.clear()
        out = by["radarr_add_movie"].func(movie="The Matrix")
        b = POSTS[f"/api/v3/movie"]
        assert out["added"] and b["qualityProfileId"] == 4, (out, b)   # preferred HD-1080p
        assert b["rootFolderPath"] == "/media" and b["monitored"] is True, b
        assert b["minimumAvailability"] == "released", b
        assert b["addOptions"]["searchForMovie"] is True, b
        assert "metadataProfileId" not in b, "radarr has no metadata profile"
        print("PASS: radarr_add_movie body (HD profile, minAvail, searchForMovie)")

        # sonarr: monitor all + searchForMissingEpisodes
        out = by["sonarr_add_series"].func(series="Breaking Bad")
        b = POSTS["/api/v3/series"]
        assert b["addOptions"]["monitor"] == "all", b
        assert b["addOptions"]["searchForMissingEpisodes"] is True, b
        print("PASS: sonarr_add_series body (monitor all, searchForMissingEpisodes)")

        # lidarr: metadataProfileId + searchForMissingAlbums (v1 path)
        out = by["lidarr_add_artist"].func(artist="Cyndi Lauper")
        b = POSTS["/api/v1/artist"]
        assert b["metadataProfileId"] == 1, b
        assert b["addOptions"]["searchForMissingAlbums"] is True, b
        assert b["qualityProfileId"] == 1, b  # lidarr prefers "Any"
        print("PASS: lidarr_add_artist body (metadata profile, searchForMissingAlbums, Any)")

        # already-in-library -> no POST
        POSTS.clear()
        out = by["radarr_add_movie"].func(movie="existing thing")
        assert out["already_in_library"] and not out["added"] and not POSTS, out
        print("PASS: already-in-library short-circuits (no POST)")

        # no match -> ModelRetry
        try:
            by["sonarr_add_series"].func(series="nothing here")
            print("FAIL: expected ModelRetry"); return 1
        except ModelRetry as e:
            assert "no series found" in str(e), str(e)
        print("PASS: no match -> teaching ModelRetry")

        print("\nALL *ARR ACQUIRE TESTS PASSED")
        return 0
    finally:
        os.unlink(mpath); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
