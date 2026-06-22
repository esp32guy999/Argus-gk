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
                          {"id": 7, "name": "Lossless"}, {"id": 9, "name": "eBook"},
                          {"id": 11, "name": "Spoken"}])
        elif p.endswith("/metadataprofile"):
            self._j(200, [{"id": 1, "name": "Standard"}])
        elif p.endswith("/rootfolder"):
            self._j(200, [{"path": "/media"}])
        elif p.endswith("/album"):   # lidarr_get_album: albums for an artist
            self._j(200, [
                {"id": 111, "title": "She’s So Unusual", "monitored": False,
                 "statistics": {"trackFileCount": 0, "trackCount": 23}},
                {"id": 112, "title": "True Colors", "monitored": True,
                 "statistics": {"trackFileCount": 10, "trackCount": 10}}])
        elif p.endswith("/artist"):   # lidarr library list (not /artist/lookup)
            self._j(200, [{"id": 13, "artistName": "Cyndi Lauper", "monitored": True}])
        else:
            self._j(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode() or "{}")
        POSTS[urlparse(self.path).path] = body
        self._j(201, {**body, "id": 99})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode() or "{}")
        POSTS[urlparse(self.path).path] = body
        self._j(202, body)


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
        assert set(by) == {"radarr_add_movie", "sonarr_add_series", "lidarr_add_artist",
                           "readarr_add_author", "lidarr_get_album"}, list(by)
        print("PASS: add tools + lidarr_get_album registered")

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

        # readarr/bookshelf: defaults to the Spoken (audiobook) profile, not eBook
        POSTS.clear()
        out = by["readarr_add_author"].func(author="Brandon Sanderson")
        b = POSTS["/api/v1/author"]
        assert b["metadataProfileId"] == 1 and b["qualityProfileId"] == 11, b  # Spoken, not eBook(9)
        assert b["addOptions"]["searchForMissingBooks"] is True, b
        print("PASS: readarr_add_author defaults to Spoken/audiobook profile")

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

        # already-in-library -> NOT a dead end: triggers a missing-items search command
        POSTS.clear()
        out = by["radarr_add_movie"].func(movie="existing thing")
        assert out["already_in_library"] and not out["added"] and out["searching"], out
        assert POSTS["/api/v3/command"]["name"] == "MoviesSearch", POSTS
        assert POSTS["/api/v3/command"]["movieIds"] == [5], POSTS
        print("PASS: already-in-library now searches missing (MoviesSearch), not 'nothing to do'")

        # lidarr_get_album: targets ONE album by an existing artist + fires AlbumSearch
        POSTS.clear()
        out = by["lidarr_get_album"].func(artist="Cyndi Lauper", album="she's so unusual")
        assert out["album"] == "She’s So Unusual" and out["searching"], out
        assert out["have_tracks"] == 0 and out["total_tracks"] == 23, out
        assert POSTS["/api/v1/album/monitor"] == {"albumIds": [111], "monitored": True}, POSTS
        assert POSTS["/api/v1/command"] == {"name": "AlbumSearch", "albumIds": [111]}, POSTS
        print("PASS: lidarr_get_album monitors + AlbumSearches just the named album")

        # fuzzy: a wrong-but-close title still lands on the real album (the user's case)
        POSTS.clear()
        out = by["lidarr_get_album"].func(artist="Cyndi Lauper", album="she's so strange")
        assert out["album"] == "She’s So Unusual", out
        print("PASS: fuzzy album match ('strange' -> 'Unusual')")

        # genuinely unknown album -> teaching ModelRetry that lists the real albums
        try:
            by["lidarr_get_album"].func(artist="Cyndi Lauper", album="Polka Party Live")
            print("FAIL: expected ModelRetry on unknown album"); return 1
        except ModelRetry as e:
            assert "She’s So Unusual" in str(e), str(e)
        print("PASS: unknown album -> teaching ModelRetry lists real albums")

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
