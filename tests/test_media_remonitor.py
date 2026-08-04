"""Contract tests for media remonitor / acquire / health (offline fake *arr).

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
    13: {
        "id": 13, "title": "Fresh Release", "year": 2026, "monitored": False,
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
    from pydantic_ai.exceptions import ModelRetry
    from argus.tools import media_remonitor, media_acquire, media_health

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
        rby = {t.name: t for t in media_remonitor.tools(mpath)}
        aby = {t.name: t for t in media_acquire.tools(mpath)}
        hby = {t.name: t for t in media_health.tools(mpath)}
        assert "media_remonitor_scan" in rby and "media_remonitor_apply" in rby
        assert "media_acquire_request" in aby
        assert "media_health_scan" in hby
        print("PASS: remonitor + acquire + health tools registered")

        scan = rby["media_remonitor_scan"].func
        apply = rby["media_remonitor_apply"].func
        acquire = aby["media_acquire_request"].func
        health = hby["media_health_scan"].func

        # --- evidence contract on scan ---
        r = scan(service="both", mode="unmonitored_missing", limit=50)
        assert r["count"] >= 2
        for c in r["candidates"]:
            assert "evidence_id" in c and c["evidence_id"].startswith("media-remonitor-")
            assert "before" in c and "monitored" in c["before"]
            assert "confidence" in c and isinstance(c["confidence"], int)
            assert "reasons" in c and len(c["reasons"]) >= 2
            assert c["decision"] in ("REMONITOR", "PROPOSE", "IGNORE")
            assert "actions_suggested" in c
            assert "set_monitored" in c["actions_suggested"] or c["before"]["monitored"]
        print("PASS: scan evidence contract (id, before, confidence, reasons, decision)")

        # confidence: fresh release unmon missing should score high
        fresh = next(c for c in r["candidates"] if c["id"] == 13)
        assert fresh["confidence"] >= 80, fresh
        assert fresh["decision"] in ("REMONITOR", "PROPOSE")
        print("PASS: confidence scoring boosts recent unmon missing")

        # complete unmon should score low / IGNORE on unmonitored mode
        r_un = scan(service="radarr", mode="unmonitored")
        complete = next(c for c in r_un["candidates"] if c["id"] == 11)
        assert complete["confidence"] < 50, complete
        assert complete["decision"] == "IGNORE"
        assert any("intentional" in x for x in complete["reasons"])
        print("PASS: complete unmonitored → low confidence IGNORE")

        # --- safety gate broad apply ---
        try:
            apply(service="both", mode="gaps", dry_run=False, limit=5)
            raise AssertionError("expected ModelRetry for broad gaps")
        except ModelRetry as e:
            assert "allow_broad_scope" in str(e)
        print("PASS: broad mode gaps blocked without allow_broad_scope")

        # dry_run broad is ok
        PUTS.clear(); POSTS.clear()
        r = apply(service="both", mode="gaps", dry_run=True, limit=20, min_confidence=0)
        assert r["dry_run"] is True
        assert PUTS == [] and POSTS == []
        print("PASS: dry_run broad gaps allowed, no writes")

        # --- monitor-only apply (no search) ---
        PUTS.clear(); POSTS.clear()
        r = apply(
            service="radarr", mode="unmonitored_missing", ids="10,13",
            dry_run=False, min_confidence=0,
        )
        assert r["applied_ok"] == 2
        assert r["acquisition"]["started"] is False
        assert POSTS == [], "remonitor must not POST search commands"
        assert MOVIES[10]["monitored"] is True and MOVIES[13]["monitored"] is True
        for ev in r["evidence"]:
            assert ev["post_condition_ok"] is True
            assert ev["after_actual"]["monitored"] is True
            assert ev.get("search_requested") is False
            assert "set_monitored" in ev["actions"]
        print("PASS: apply is monitor-only with post_condition evidence")

        # sonarr episodes S1+ monitored, season 0 skipped
        PUTS.clear(); POSTS.clear()
        r = apply(service="sonarr", mode="unmonitored_missing", ids="1", dry_run=False, min_confidence=0)
        assert SERIES[1]["monitored"] is True
        assert EPISODES[1][0]["monitored"] is True
        assert EPISODES[1][2]["monitored"] is False
        assert POSTS == []
        print("PASS: sonarr remonitor episodes; no acquire; skip S0")

        # min_confidence filters
        r = apply(
            service="radarr", mode="unmonitored", ids="11",
            dry_run=True, min_confidence=90,
        )
        assert r["planned"] == 0 and r["skipped_low_confidence"] >= 1
        print("PASS: min_confidence skips low-score candidates")

        # --- acquire boundary ---
        POSTS.clear()
        # unmonitored still fails for id 11
        r = acquire(service="radarr", ids="11", dry_run=False)
        assert r["ok"] == 0 and not r["results"][0]["ok"]
        assert "not monitored" in r["results"][0]["error"].lower() or "remonitor" in r["results"][0]["error"].lower()
        print("PASS: acquire refuses unmonitored entity")

        # monitored missing → search
        POSTS.clear()
        r = acquire(service="radarr", ids="12", dry_run=False)
        assert r["searching"] == 1
        assert any(b.get("name") == "MoviesSearch" for _, b in POSTS)
        assert r["evidence"][0]["decision"] == "ACQUIRE"
        assert r["evidence"][0]["search_requested"] is True
        print("PASS: acquire queues MoviesSearch with evidence")

        # dry_run acquire
        POSTS.clear()
        r = acquire(service="radarr", ids="12", dry_run=True)
        assert r["dry_run"] is True and POSTS == []
        print("PASS: acquire dry_run writes nothing")

        # ids required
        try:
            acquire(service="radarr", ids="")
            raise AssertionError("expected ModelRetry")
        except ModelRetry:
            pass
        print("PASS: acquire requires ids")

        # batch >10 needs broad scope
        try:
            acquire(service="radarr", ids=",".join(str(i) for i in range(1, 12)), dry_run=False)
            raise AssertionError("expected broad scope ModelRetry")
        except ModelRetry as e:
            assert "allow_broad_scope" in str(e)
        print("PASS: large acquire batch needs allow_broad_scope")

        # --- health auditor ---
        h = health(service="both", limit=50)
        assert "stats" in h and "recommendations" in h
        assert "movies" in h["stats"] and "tv" in h["stats"]
        rec = h["recommendations"]
        assert set(rec) >= {"REMONITOR", "REVIEW", "IGNORE", "ACQUIRE"}
        # monitored missing movie 12 → ACQUIRE bucket
        acq_ids = {i["id"] for i in rec["ACQUIRE"]["items"] if i["service"] == "radarr"}
        assert 12 in acq_ids
        # unmon complete 11 → IGNORE
        ign_ids = {i["id"] for i in rec["IGNORE"]["items"] if i.get("service") == "radarr"}
        assert 11 in ign_ids
        print("PASS: media_health_scan buckets REMONITOR/REVIEW/IGNORE/ACQUIRE")

        # explanation fields present on health items
        sample = (rec["ACQUIRE"]["items"] or rec["REMONITOR"]["items"] or rec["REVIEW"]["items"])[0]
        assert any("monitored=" in x for x in sample["reasons"])
        print("PASS: explanation reasons on health evidence")

        print("\nALL MEDIA REMONITOR/ACQUIRE/HEALTH TESTS PASSED")
        return 0
    finally:
        srv.shutdown()
        os.unlink(mpath)


if __name__ == "__main__":
    sys.exit(main())
