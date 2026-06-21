"""Contract test for the Navidrome/Subsonic lane (argus/tools/navidrome.py).

A local fake Subsonic server validates the salt+token auth on every request and
returns canned responses, so this exercises the real client (auth, endpoints,
DJ build = delete-then-create). Offline. python tests/test_navidrome_provider.py
"""
from __future__ import annotations
import hashlib, json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USER, PW = "tester", "s3cret"
CALLS = []   # (endpoint, params) seen by the server


def _resp(obj):
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", **obj}}


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path)
        ep = u.path.rsplit("/", 1)[-1].replace(".view", "")
        q = {k: (v[0] if len(v) == 1 else v) for k, v in parse_qs(u.query).items()}
        CALLS.append((ep, q))
        # --- auth check on EVERY request ---
        salt = q.get("s", "")
        expect = hashlib.md5((PW + salt).encode()).hexdigest()
        if q.get("u") != USER or q.get("t") != expect or not q.get("v") or q.get("f") != "json":
            return self._json({"subsonic-response": {"status": "failed",
                               "error": {"code": 40, "message": "auth"}}})
        if ep == "getPlaylists":
            self._json(_resp({"playlists": {"playlist": [
                {"id": "p1", "name": "Old", "songCount": 3},
                {"id": "p2", "name": "🎧 jazz", "songCount": 5}]}}))
        elif ep == "getGenres":
            self._json(_resp({"genres": {"genre": [
                {"value": "Jazz", "songCount": 100}, {"value": "Rock", "songCount": 50}]}}))
        elif ep == "getRandomSongs":
            self._json(_resp({"randomSongs": {"song": [
                {"id": "s1", "title": "Blue", "artist": "Miles"},
                {"id": "s2", "title": "Green", "artist": "Coltrane"}]}}))
        elif ep == "search3":
            self._json(_resp({"searchResult3": {"song": [
                {"id": "s9", "title": "Found", "artist": "X"}]}}))
        elif ep == "createPlaylist":
            self._json(_resp({"playlist": {"id": "new1", "name": q.get("name"), "songCount": 2}}))
        elif ep == "deletePlaylist":
            self._json(_resp({}))
        else:
            self._json(_resp({}))

    def _json(self, obj):
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(json.dumps(obj).encode())


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import navidrome

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    fd, cfg = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, f"base_url: http://127.0.0.1:{port}\nusername: {USER}\npassword: {PW}\ndefault_count: 30\n".encode())
    os.close(fd)
    try:
        by = {t.name: t for t in navidrome.tools(cfg)}
        assert {"navidrome_list_playlists", "navidrome_list_genres",
                "navidrome_dj", "navidrome_create_playlist"} <= set(by), list(by)
        for t in by.values():
            assert t.provider == "navidrome", t.provider
        print("PASS: lane exposes the expected tools")

        # auth: a real call succeeds (server rejects bad token) -> proves salt+token
        pls = by["navidrome_list_playlists"].func()
        assert any(p["name"] == "Old" for p in pls), pls
        print("PASS: salt+token auth accepted; list_playlists works")

        genres = by["navidrome_list_genres"].func()
        assert "Jazz" in genres and "Rock" in genres, genres
        print("PASS: list_genres returns genre names")

        CALLS.clear()
        out = by["navidrome_dj"].func(genre="jazz")
        # DJ must: fetch random songs by genre, and (since "🎧 jazz" exists) delete then create
        eps = [c[0] for c in CALLS]
        assert "getRandomSongs" in eps, eps
        assert "deletePlaylist" in eps, f"DJ should refresh (delete existing): {eps}"
        assert "createPlaylist" in eps, eps
        rnd = next(c for c in CALLS if c[0] == "getRandomSongs")[1]
        assert rnd.get("genre") == "jazz", rnd
        assert out.get("count") == 2 and len(out.get("tracks", [])) == 2, out
        print("PASS: navidrome_dj builds by genre + refreshes existing playlist")

        CALLS.clear()
        c = by["navidrome_create_playlist"].func(name="Roadtrip", query="found")
        eps = [x[0] for x in CALLS]
        assert "search3" in eps and "createPlaylist" in eps, eps
        assert c.get("count") == 1, c
        print("PASS: navidrome_create_playlist searches + creates")

        print("\nALL NAVIDROME CONTRACT TESTS PASSED")
        return 0
    finally:
        os.unlink(cfg); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
