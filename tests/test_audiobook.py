"""Contract test for the audiobook lane (argus/tools/audiobook.py).

A fake audiobook-getter validates: search ranking (audiobook over ebook, then
seeders), get -> POST /api/grab with the chosen result, and error handling. Offline.
    python tests/test_audiobook.py
"""
from __future__ import annotations
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GRABBED = {}


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _j(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if urlparse(self.path).path == "/api/search":
            self._j(200, {"results": [
                {"title": "Project Hail Mary by Andy Weir EPUB", "source_type": "torrent",
                 "source": "LimeTorrents", "seeders": 437, "size_human": "9 MB"},
                {"title": "Project Hail Mary (Unabridged)", "source_type": "torrent_abb",
                 "source": "AudiobookBay", "seeders": 50, "size_human": "600 MB",
                 "abb_page_url": "http://abb/x"},
                {"title": "Project Hail Mary audiobook", "source_type": "torrent_abb",
                 "source": "AudiobookBay", "seeders": 120, "size_human": "610 MB",
                 "abb_page_url": "http://abb/y"},
            ], "count": 3})
        elif urlparse(self.path).path == "/api/empty":
            self._j(200, {"results": []})
        else:
            self._j(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        GRABBED.clear(); GRABBED.update(json.loads(self.rfile.read(n).decode() or "{}"))
        self._j(200, {"ok": True})


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import audiobook

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    audiobook.BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        by = {t.name: t for t in audiobook.tools()}
        assert set(by) == {"audiobook_search", "audiobook_get"}, list(by)
        assert by["audiobook_get"].provider == "audiobook"
        print("PASS: audiobook tools registered")

        # search ranks the highest-seeded AUDIOBOOK first (not the 437-seed EPUB)
        res = audiobook.audiobook_search("project hail mary")
        assert res[0]["audiobook"] is True and "Unabridged" in res[0]["title"] or "audiobook" in res[0]["title"].lower(), res[0]
        assert res[0]["seeders"] == 120, res[0]   # top audiobook by seeders
        assert any("EPUB" in r["title"] for r in res), "ebook still listed, just ranked lower"
        print("PASS: search ranks audiobook over ebook, then by seeders")

        # get -> grabs the chosen audiobook (POSTs the full result dict)
        out = audiobook.audiobook_get("project hail mary")
        assert out["grabbed"] is True, out
        assert GRABBED.get("source_type") == "torrent_abb", GRABBED
        assert GRABBED.get("seeders") == 120, GRABBED
        print("PASS: get posts the chosen audiobook to /api/grab")

        # no results -> teaching ModelRetry
        audiobook.BASE = audiobook.BASE  # same server; hit empty path via monkeypatch
        orig = audiobook._search
        audiobook._search = lambda q: []
        try:
            audiobook.audiobook_get("nothing")
            print("FAIL: expected ModelRetry on no results"); return 1
        except ModelRetry as e:
            assert "no audiobooks found" in str(e), str(e)
        finally:
            audiobook._search = orig
        print("PASS: no results -> teaching ModelRetry")

        print("\nALL AUDIOBOOK TESTS PASSED")
        return 0
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
