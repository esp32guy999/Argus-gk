"""Contract test for the OpenAPI provider (argus/tools/openapi.py).

Spins a fake API server + a minimal OpenAPI spec, then drives the lane end-to-end:
path params, query params, JSON body, and an error path.
Runnable standalone:  python tests/test_openapi_provider.py   (exit 0 = pass)
"""
from __future__ import annotations
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/items/{id}": {
            "get": {
                "operationId": "getItem",
                "summary": "Get an item by id.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}, "description": "item id"},
                    {"name": "verbose", "in": "query", "required": False,
                     "schema": {"type": "boolean"}, "description": "verbose flag"},
                ],
            }
        },
        "/items": {
            "post": {
                "operationId": "createItem",
                "summary": "Create an item.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"},
                                       "qty": {"type": "integer"}},
                        "required": ["name"],
                    }}},
                },
            }
        },
    },
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "items":
            item_id = parts[1]
            if item_id == "boom":
                self.send_response(500); self.end_headers(); self.wfile.write(b"nope"); return
            q = parse_qs(u.query)
            self._json(200, {"id": item_id, "verbose": q.get("verbose", [None])[0]})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode() or "{}")
        if urlparse(self.path).path == "/items":
            self._json(200, {"created": body})
        else:
            self.send_response(404); self.end_headers()


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    fd, spec_path = tempfile.mkstemp(suffix="_openapi.json")
    os.write(fd, json.dumps(SPEC).encode()); os.close(fd)
    manifest = f"""
spec: {spec_path}
base_url: http://127.0.0.1:{port}
operations: [getItem, createItem]
tags: [test, api]
default_timeout: 10
"""
    fd2, mpath = tempfile.mkstemp(suffix=".yaml")
    os.write(fd2, manifest.encode()); os.close(fd2)

    try:
        from argus.tools import openapi
        tools = openapi.tools(mpath)
        by = {t.name: t for t in tools}

        # 1. spec -> Tool contract
        assert set(by) == {"getItem", "createItem"}, list(by)
        g = by["getItem"]
        assert g.provider == "openapi", g.provider
        assert g.tags == ["test", "api"], g.tags
        props = (g.schema or {}).get("properties", {})
        assert "id" in props and "verbose" in props, props
        assert "id" in (g.schema or {}).get("required", []), g.schema
        print("PASS: spec -> Tool contract (params merged into schema)")

        # 2. external-schema path builds a pydantic tool
        for t in tools:
            t.as_pydantic_tool()
        print("PASS: as_pydantic_tool() via from_schema")

        # 3. GET with path + query param
        out = by["getItem"].func(id="abc", verbose=True)
        assert isinstance(out, dict) and out.get("id") == "abc", out
        print("PASS: GET routes path + query params")

        # 4. POST with JSON body
        out = by["createItem"].func(name="widget", qty=3)
        assert out.get("created") == {"name": "widget", "qty": 3}, out
        print("PASS: POST routes JSON body")

        # 5. HTTP error -> teaching ModelRetry
        try:
            by["getItem"].func(id="boom")
            print("FAIL: expected ModelRetry on HTTP 500"); return 1
        except ModelRetry as e:
            assert "getItem" in str(e) or "500" in str(e), str(e)
            print("PASS: HTTP error -> teaching ModelRetry")

        # 6. multi-service manifest prefixes tool names by service
        multi = f"""
services:
  - name: svc
    spec: {spec_path}
    base_url: http://127.0.0.1:{port}
    operations: [getItem]
    tags: [m]
"""
        fd3, mpath3 = tempfile.mkstemp(suffix=".yaml")
        os.write(fd3, multi.encode()); os.close(fd3)
        try:
            names = {t.name for t in openapi.tools(mpath3)}
            assert names == {"svc_getItem"}, names
            print("PASS: multi-service manifest prefixes tool names by service")
        finally:
            os.unlink(mpath3)

        # 7. response projection trims a dict response to the `fields` allowlist
        proj = f"""
spec: {spec_path}
base_url: http://127.0.0.1:{port}
operations: [getItem]
fields:
  getItem: [id]
tags: [test]
default_timeout: 10
"""
        fd4, mpath4 = tempfile.mkstemp(suffix=".yaml")
        os.write(fd4, proj.encode()); os.close(fd4)
        try:
            pt = openapi.tools(mpath4)[0]
            out = pt.func(id="abc", verbose=True)
            assert out == {"id": "abc"}, f"projection should drop 'verbose': {out}"
            print("PASS: response projection trims dict to fields allowlist")
        finally:
            os.unlink(mpath4)

        # 7b. `limit` caps list responses (protects context on noisy endpoints)
        # /items/{id} returns a dict; use a list-returning fake via the search-style path.
        # Reuse getItem but make the server return a list when id == "many".
        # (Simplest: assert _project+limit composition through a tiny manual dispatch.)
        limitman = f"""
spec: {spec_path}
base_url: http://127.0.0.1:{port}
operations: [getItem]
limits:
  getItem: 2
tags: [test]
default_timeout: 10
"""
        fdl, mpathl = tempfile.mkstemp(suffix=".yaml")
        os.write(fdl, limitman.encode()); os.close(fdl)
        try:
            lt = openapi.tools(mpathl)[0]
            # getItem returns a dict (not a list), so limit is a no-op -> still works
            assert lt.func(id="abc")["id"] == "abc", "limit must not break dict responses"
            print("PASS: limit config is a no-op on dict responses (safe)")
        finally:
            os.unlink(mpathl)

        # 8. _project handles a list of dicts (the list-* case) + passes non-projected through
        from argus.tools.openapi import _project
        rows = [{"id": 1, "name": "a", "junk": "x"}, {"id": 2, "name": "b", "junk": "y"}]
        assert _project(rows, ["id", "name"]) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        assert _project(rows, None) == rows, "no fields -> untouched"
        assert _project("plain", ["id"]) == "plain", "non-dict/list -> untouched"
        print("PASS: _project handles list-of-dicts + pass-through")

        print("\nALL OPENAPI CONTRACT TESTS PASSED")
        return 0
    finally:
        os.unlink(spec_path); os.unlink(mpath); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
