"""Contract tests for media remonitor lane (offline fake *arr server).

python tests/test_media_remonitor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mutable library state the fake server mutates on PUT
SERIES = {
    1: {
        "id": 1, "title": "Ended Show", "year": 2020, "monitored": False, "status": "ended",
        "statistics": {"episodeFileCount": 0, "episodeCount": 5, "totalEpisodeCount": 5},
    },
    2: {
        "id": 2, "title": "Complete Monitored", "year": 2021, "monitored": True, "status": "ended",
        "statistics": {"episodeFileCount": 10, "episodeCount": 10, "totalEpisodeCount": 10},
    },
    3: {
        "id": 3, "title": "Monitored Missing", "year": 2022, "monitored": True, "status": "ended",
        "statistics": {"episodeFileCount": 2, "episodeCount": 8, "totalEpisodeCount": 8},
    },
}
MOVIES = {
    10: {
        "id": 10, "title": "Unmon Missing", "year": 2024, "monitored": False,
        "hasFile": False, "status": "released",
    },
    11: {
        "id": 11, "title": "Unmon Complete", "year": 2010, "monitored": False,
        "hasFile": True, "status": "released",
    },
    12: {
        "id": 12, "title": "Mon Missing", "year": 2015, "monitored": True,
        "hasFile": False, "status": "released",
    },
}
EPISODES = {
    1: [
        {"id": 101, "seasonNumber": 1, "episodeNumber": 1, "monitored": False, "hasFile": False},
        {"id": 102, "seasonNumber": 1, "episodeNumber": 2, "monitored": False, "hasFile": False},
        {"id": 103, "seasonNumber": 0, "episodeNumber": 1, "monitored": False, "hasFile": False},
    ],
    3: [
        {"id": 301, "seasonNumber": 1, "episodeNumber": 1, "monitored": True, "hasFile": True},
        {"id": 302, "seasonNumber": 1, "episodeNumber": 2, "monitored": True, "hasFile": False},
    ],
}
PUTS: list[tuple[str, dict]] = []
POSTS: list[tuple[str, dict]] = []


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == "/api/v3/series":
            self._j(200, list(SERIES.values()))
        elif p.startswith("/api/v3/series/"):
            sid = int(p.rsplit("/", 1)[-1])
            self._j(200, SERIES[sid])
        elif p == "/api/v3/episode":
            sid = int(q.get("seriesId", ["0"])[0])
            self._j(200, EPISODES.get(sid, []))
        elif p == "/api/v3/movie":
            self._j(200, list(MOVIES.values()))
        elif p.startswith("/api/v3/movie/"):
            mid = int(p.rsplit("/", 1)[-1])
            self._j(200, MOVIES[mid])
        else:
            self._j(404, {"error": p})

    def do_PUT(self):
        body = self._read()
        p = urlparse(self.path).path
        PUTS.append((p, body))
        if p.startswith("/api/v3/series/"):
            sid = int(p.rsplit("/", 1)[-1])
            SERIES[sid] = {**SERIES[sid], **body, "id": sid}
            self._j(202, SERIES[sid])
        elif p == "/api/v3/episode/monitor":
            for eid in body.get("episodeIds", []):
                for eps in EPISODES.values():
                    for e in eps:
                        if e["id"] == eid and body.get("monitored"):
                            e["monitored"] = True
            self._j(202, body)
        elif p.startswith("/api/v3/movie/"):
            mid = int(p.rsplit("/", 1)[-1])
            MOVIES[mid] = {**MOVIES[mid], **body, "id": mid}
            self._j(202, MOVIES[mid])
        else:
            self._j(404, {})

    def do_POST(self):
        body = self._read()
        p = urlparse(self.path).path
        POSTS.append((p, body))
        self._j(201, {**body, "id": 999})


def main() -> int:
    from argus.tools import media_remonitor

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    fd, mpath = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, (
        "services:\n"
        f"  - name: sonarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n"
        f"  - name: radarr\n    base_url: {base}\n    headers: {{X-Api-Key: k}}\n"
    ).encode())
    os.close(fd)

    try:
        tools = media_remonitor.tools(mpath)
        by = {t.name: t for t in tools}
        assert set(by) == {"media_remonitor_scan", "media_remonitor_apply"}, list(by)
        print("PASS: tools registered")

        scan = by["media_remonitor_scan"].func
        apply = by["media_remonitor_apply"].func

        # default mode: unmonitored + missing
        r = scan(service="both", mode="unmonitored_missing", limit=50)
        ids = {(c["service"], c["id"]) for c in r["candidates"]}
        assert ("sonarr", 1) in ids, r
        assert ("radarr", 10) in ids, r
        assert ("radarr", 11) not in ids  # unmon but has file
        assert ("sonarr", 2) not in ids
        assert ("radarr", 12) not in ids  # monitored missing — not unmonitored_missing
        print("PASS: scan unmonitored_missing filters correctly")

        r = scan(service="radarr", mode="missing")
        ids = {c["id"] for c in r["candidates"]}
        assert ids == {10, 12}, ids
        print("PASS: scan radarr missing")

        r = scan(service="sonarr", mode="gaps")
        ids = {c["id"] for c in r["candidates"]}
        assert 1 in ids and 3 in ids and 2 not in ids, ids
        print("PASS: scan sonarr gaps")

        r = scan(service="both", mode="unmonitored")
        titles = {c["title"] for c in r["candidates"]}
        assert "Unmon Complete" in titles  # has file but unmon
        print("PASS: scan unmonitored includes complete unmon")

        # dry_run apply — no PUT/POST
        PUTS.clear(); POSTS.clear()
        r = apply(service="both", mode="unmonitored_missing", dry_run=True, search=True)
        assert r["dry_run"] is True
        assert r["planned"] >= 2
        assert all(x.get("ok") for x in r["results"])
        assert PUTS == [] and POSTS == []
        print("PASS: dry_run apply writes nothing")

        # real apply — remonitor + search
        PUTS.clear(); POSTS.clear()
        r = apply(service="both", mode="unmonitored_missing", dry_run=False, search=True)
        assert r["applied_ok"] == r["planned"]
        assert SERIES[1]["monitored"] is True
        assert MOVIES[10]["monitored"] is True
        # episodes S1+ monitored; season 0 left alone
        assert EPISODES[1][0]["monitored"] is True
        assert EPISODES[1][1]["monitored"] is True
        assert EPISODES[1][2]["monitored"] is False  # special
        cmds = [b.get("name") for _, b in POSTS if _.endswith("/command")]
        assert "SeriesSearch" in cmds and "MoviesSearch" in cmds, cmds
        print("PASS: apply remonitors + searches; skips season 0")

        # ids filter
        PUTS.clear(); POSTS.clear()
        # reset movie 12 still mon missing — use missing mode on id 12 only
        r = apply(service="radarr", mode="missing", ids="12", dry_run=False, search=True)
        assert r["planned"] == 1 and r["results"][0]["id"] == 12
        assert any(b.get("name") == "MoviesSearch" for _, b in POSTS)
        print("PASS: ids filter targets one movie")

        # invalid mode teaches
        try:
            scan(mode="nope")
            raise AssertionError("expected ModelRetry")
        except Exception as e:
            assert "mode" in str(e).lower() or "ModelRetry" in type(e).__name__
            from pydantic_ai.exceptions import ModelRetry
            assert isinstance(e, ModelRetry)
        print("PASS: invalid mode -> ModelRetry")

        print("\nALL MEDIA_REMONITOR TESTS PASSED")
        return 0
    finally:
        srv.shutdown()
        os.unlink(mpath)


if __name__ == "__main__":
    sys.exit(main())
