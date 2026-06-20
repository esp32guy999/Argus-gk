"""Contract test for the web lane (argus/tools/web.py).

Offline: a local HTTP server stands in for a website (real httpx for web_fetch);
the Brave/DDG parsers are tested as pure functions. Runnable standalone:
    python tests/test_web_provider.py     (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAGE = b"""<html><head><title>PETG Guide</title><style>.x{color:red}</style></head>
<body><nav>menu junk</nav>
<h1>PETG Settings</h1>
<p>Nozzle temp is 240C for the P1S.</p>
<script>console.log('tracking junk')</script>
<p>Bed temp is 70C.</p>
<footer>copyright junk</footer></body></html>"""


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/page":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(PAGE)
        elif self.path == "/boom":
            self.send_response(500); self.end_headers(); self.wfile.write(b"err")
        else:
            self.send_response(404); self.end_headers()


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import web

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        # 1. html -> text: keeps content, drops script/style/nav/footer, gets title
        title, text = web._html_to_text(PAGE.decode())
        assert title == "PETG Guide", title
        assert "240C" in text and "Bed temp is 70C" in text, text
        assert "tracking junk" not in text and "menu junk" not in text and "copyright" not in text, text
        print("PASS: _html_to_text extracts content, drops boilerplate + script/style")

        # 2. web_fetch over a real socket
        out = web.web_fetch(f"http://127.0.0.1:{port}/page")
        assert out["title"] == "PETG Guide" and "240C" in out["text"], out
        assert out["truncated"] is False, out
        print("PASS: web_fetch returns title + readable text")

        # 3. web_fetch truncates to the configured cap
        web._FETCH_MAX = 20
        out2 = web.web_fetch(f"http://127.0.0.1:{port}/page")
        assert len(out2["text"]) <= 20 and out2["truncated"] is True, out2
        web._FETCH_MAX = 6000
        print("PASS: web_fetch truncates + flags")

        # 4. HTTP error -> teaching ModelRetry
        try:
            web.web_fetch(f"http://127.0.0.1:{port}/boom")
            print("FAIL: expected ModelRetry on HTTP 500"); return 1
        except ModelRetry as e:
            assert "500" in str(e), str(e)
        print("PASS: web_fetch HTTP error -> teaching ModelRetry")

        # 5. Brave result parsing (pure)
        brave = {"web": {"results": [
            {"title": "T1", "url": "https://a", "description": "snip <b>one</b>"},
            {"title": "T2", "url": "https://b", "description": "snip two"},
        ]}}
        hits = web._parse_brave(brave)
        assert len(hits) == 2 and hits[0]["url"] == "https://a", hits
        assert hits[0]["snippet"] == "snip one", hits[0]  # html stripped from snippet
        print("PASS: _parse_brave shapes hits + strips html")

        # 6. tools registered with web tags
        by = {t.name: t for t in web.tools()}
        assert set(by) == {"web_search", "web_fetch"}, list(by)
        assert "internet" in by["web_search"].tags and "browse" in by["web_fetch"].tags
        print("PASS: web_search + web_fetch registered with tags")

        print("\nALL WEB CONTRACT TESTS PASSED")
        return 0
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
