"""Contract test for the Lidarr acquisition lane (argus/tools/lidarr.py).

A fake Lidarr validates the add flow: lookup -> profiles + rootfolder -> POST artist
with the right body (monitor + searchForMissingAlbums), plus already-added handling.
Offline. python tests/test_lidarr_acquire.py
"""
from __future__ import annotations
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

POSTED = {}   # captures the POST /artist body


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/v1/artist/lookup":
            term = q.get("term", [""])[0].lower()
            if "cyndi" in term:
                self._json(200, [{"artistName": "Cyndi Lauper",
                                  "foreignArtistId": "fid-cyndi", "monitored": False}])
            elif "grateful" in term:  # already-in-library: has a real id
                self._json(200, [{"artistName": "Grateful Dead", "id": 7,
                                  "foreignArtistId": "fid-gd"}])
            else:
                self._json(200, [])
        elif u.path == "/api/v1/qualityprofile":
            self._json(200, [{"id": 1, "name": "Any"}, {"id": 3, "name": "Standard"}])
        elif u.path == "/api/v1/metadataprofile":
            self._json(200, [{"id": 1, "name": "Standard"}, {"id": 2, "name": "None"}])
        elif u.path == "/api/v1/rootfolder":
            self._json(200, [{"path": "/music"}])
        else:
            self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode() or "{}")
        if urlparse(self.path).path == "/api/v1/artist":
            POSTED.clear(); POSTED.update(body)
            self._json(201, {**body, "id": 99})
        else:
            self._json(404, {})


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import lidarr

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    fd, mpath = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, (f"services:\n  - name: lidarr\n    base_url: http://127.0.0.1:{port}\n"
                  f"    headers: {{X-Api-Key: testkey}}\n").encode())
    os.close(fd)
    try:
        add = {t.name: t for t in lidarr.tools(mpath)}["lidarr_add_artist"]
        assert add.provider == "lidarr" and "acquire" in add.tags, add.tags
        print("PASS: lidarr_add_artist registered")

        # 1. add a new artist -> POSTs an enriched body with search enabled
        out = add.func(artist="Cyndi Lauper")
        assert out["added"] is True and out["artist"] == "Cyndi Lauper", out
        assert POSTED.get("foreignArtistId") == "fid-cyndi", POSTED
        assert POSTED.get("qualityProfileId") == 1, POSTED          # preferred "Any"
        assert POSTED.get("metadataProfileId") == 1, POSTED          # preferred "Standard"
        assert POSTED.get("rootFolderPath") == "/music", POSTED
        assert POSTED.get("monitored") is True, POSTED
        assert POSTED["addOptions"]["searchForMissingAlbums"] is True, POSTED
        print("PASS: add posts enriched body (profiles, root, monitor, search)")

        # 2. already-in-library -> no POST, reports it
        POSTED.clear()
        out2 = add.func(artist="Grateful Dead")
        assert out2["already_in_library"] is True and out2["added"] is False, out2
        assert not POSTED, "should NOT post when already added"
        print("PASS: already-in-library short-circuits (no POST)")

        # 3. no match -> teaching ModelRetry
        try:
            add.func(artist="zzz nonexistent zzz")
            print("FAIL: expected ModelRetry on no match"); return 1
        except ModelRetry as e:
            assert "no artist found" in str(e), str(e)
        print("PASS: no match -> teaching ModelRetry")

        print("\nALL LIDARR ACQUIRE TESTS PASSED")
        return 0
    finally:
        os.unlink(mpath); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
