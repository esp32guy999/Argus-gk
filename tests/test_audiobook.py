"""Contract test for the audiobook lane (argus/tools/audiobook.py).

Fake ABB site (search HTML + book page with an info hash) + fake qBittorrent, so the
full ABB->magnet->qBt path is exercised offline. python tests/test_audiobook.py
"""
from __future__ import annotations
import os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HASH = "a" * 40
SEARCH_HTML = """<html><body>
<div id="content">
<div class="post"><h2><a href="/abss/project-hail-mary-andy-weir/">Project Hail Mary - Andy Weir (Unabridged)</a></h2>
  <a href="/abss/project-hail-mary-andy-weir/">Audiobook Details</a></div>
<div class="post"><h2><a href="/abss/phm-epub/">Project Hail Mary EPUB</a></h2></div>
</div>
<div id="sidebar"><div class="post"><h2><a href="/abss/latest-upload/">SOME LATEST UPLOAD</a></h2></div></div>
</body></html>"""
PAGE_HTML = f"""<html><body><table>
<tr><td>Info Hash:</td><td>{HASH}</td></tr></table></body></html>"""

QBT = {"logged_in": False, "added": None, "add_response": b"Ok."}


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" and "s" in parse_qs(u.query):
            q = parse_qs(u.query).get("s", [""])[0]
            body = (b"<html><body></body></html>" if "nothing" in q
                    else SEARCH_HTML.encode())
        elif u.path.startswith("/abss/"):
            body = PAGE_HTML.encode()
        elif u.path == "/?s=" or u.path == "/":
            body = b"<html></html>"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        if self.path == "/api/v2/auth/login":
            QBT["logged_in"] = True
            self.send_response(200); self.end_headers(); self.wfile.write(b"Ok.")
        elif self.path == "/api/v2/torrents/add":
            QBT["added"] = raw
            self.send_response(200); self.end_headers(); self.wfile.write(QBT["add_response"])
        else:
            self.send_response(404); self.end_headers()


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import audiobook
    audiobook._MIN_INTERVAL = 0   # disable politeness throttle for fast tests

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    fd, cfg = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, (f"abb_base: {base}\nqbt_url: {base}\nqbt_user: u\nqbt_pass: p\n"
                  f"category: audiobooks\ntrackers: [udp://tr.example:1337/announce]\n").encode())
    os.close(fd)
    try:
        by = {t.name: t for t in audiobook.tools(cfg)}
        assert set(by) == {"audiobook_search", "audiobook_get"}, list(by)
        assert by["audiobook_get"].provider == "audiobook"
        print("PASS: audiobook tools registered")

        # search scrapes ABB #content -> title links only (sidebar + Details excluded)
        res = by["audiobook_search"].func(query="project hail mary")
        assert any("Andy Weir" in r["title"] for r in res), res
        assert not any("LATEST UPLOAD" in r["title"] for r in res), "sidebar must be excluded"
        assert not any(r["title"] == "Audiobook Details" for r in res), "Details link excluded"
        print("PASS: audiobook_search scrapes #content results (no sidebar/Details)")

        # get: scrape hash -> build magnet -> login + add to qBt
        out = by["audiobook_get"].func(query="project hail mary")
        assert out["grabbed"] is True and out["source"] == "AudiobookBay", out
        assert QBT["logged_in"] is True, "must log in to qBt"
        assert HASH in QBT["added"] and "magnet" in QBT["added"], QBT["added"]
        assert "tr.example" in QBT["added"], "magnet should carry trackers"
        print("PASS: audiobook_get scrapes hash -> magnet -> qBt add")

        # qBt returns HTTP 200 with body "Fails." when it REJECTS a magnet — this must
        # NOT read as success (the hollow-success fix).
        QBT["add_response"] = b"Fails."
        try:
            by["audiobook_get"].func(query="project hail mary")
            print("FAIL: qBt 'Fails.' should raise, not report grabbed"); return 1
        except ModelRetry as e:
            assert "did not accept" in str(e), str(e)
        QBT["add_response"] = b"Ok."
        print("PASS: qBt 200 'Fails.' -> ModelRetry, not hollow success")

        # no results -> teaching ModelRetry
        try:
            by["audiobook_search"].func(query="zzz nothing zzz")
            print("FAIL: expected ModelRetry on no results"); return 1
        except ModelRetry as e:
            assert "no audiobooks found" in str(e), str(e)
        print("PASS: empty search -> teaching ModelRetry")

        # throttle actually enforces a minimum gap between ABB requests
        # (file-based: wall-clock timestamp in _TS_FILE, shared across processes)
        import time as _t
        audiobook._MIN_INTERVAL = 0.4
        audiobook._STATE.mkdir(parents=True, exist_ok=True)
        audiobook._TS_FILE.write_text("0")  # forget any prior request
        for f in audiobook._CACHE_DIR.glob("*.html"):
            f.unlink()  # cache hits bypass the throttle — start cold
        t0 = _t.monotonic()
        by["audiobook_search"].func(query="project hail mary")  # 1st: no wait
        # distinct query string (different cache key) that still matches the fake results
        by["audiobook_search"].func(query="hail mary weir")     # 2nd waits >= 0.4s
        assert _t.monotonic() - t0 >= 0.4, "throttle should space requests"
        audiobook._MIN_INTERVAL = 0
        print("PASS: politeness throttle enforces min interval")

        # identical repeat query is served from the disk cache — instant, no throttle
        audiobook._MIN_INTERVAL = 5.0
        t0 = _t.monotonic()
        by["audiobook_search"].func(query="hail mary weir")
        assert _t.monotonic() - t0 < 1.0, "repeat query should be a cache hit"
        audiobook._MIN_INTERVAL = 0
        print("PASS: repeat query served from cache")

        print("\nALL AUDIOBOOK TESTS PASSED")
        return 0
    finally:
        os.unlink(cfg); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
