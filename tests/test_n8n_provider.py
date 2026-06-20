"""Contract test for the n8n provider (argus/tools/n8n.py).

Defines the interface the n8n lane MUST satisfy. Runnable standalone:
    python tests/test_n8n_provider.py     (exit 0 = pass)
Spins a local HTTP server as a fake n8n webhook host, so it exercises real httpx.
"""
from __future__ import annotations
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# make 'argus' importable when run from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode() if n else ""
        if self.path == "/webhook/echo":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"received": json.loads(body or "{}")}).encode())
        elif self.path == "/webhook/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"kaboom")
        else:
            self.send_response(404)
            self.end_headers()


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry

    srv, port = _serve()
    manifest = f"""
base_url: http://127.0.0.1:{port}
default_timeout: 10
tools:
  - name: echo_tool
    description: Echo back the input.
    tags: [test, echo]
    path: /webhook/echo
    schema:
      type: object
      properties:
        item: {{type: string, description: "text to echo"}}
      required: [item]
    example: {{item: "hi"}}
  - name: boom_tool
    description: Always errors.
    tags: [test]
    path: /webhook/boom
    schema:
      type: object
      properties:
        x: {{type: string}}
      required: [x]
"""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    os.write(fd, manifest.encode()); os.close(fd)
    try:
        from argus.tools import n8n
        tools = n8n.tools(path)

        # 1. manifest -> Tool contract
        assert len(tools) == 2, f"expected 2 tools, got {len(tools)}"
        by = {t.name: t for t in tools}
        assert set(by) == {"echo_tool", "boom_tool"}, list(by)
        echo = by["echo_tool"]
        assert echo.provider == "n8n", echo.provider
        assert echo.tags == ["test", "echo"], echo.tags
        assert echo.schema and echo.schema.get("type") == "object", echo.schema
        print("PASS: manifest -> Tool contract")

        # 2. builds a pydantic tool via the external-schema path
        for t in tools:
            t.as_pydantic_tool()
        print("PASS: as_pydantic_tool() via from_schema")

        # 3. dispatch success: POSTs kwargs as JSON, returns parsed response
        out = echo.func(item="hello")
        assert isinstance(out, dict) and out.get("received") == {"item": "hello"}, out
        print("PASS: dispatch POSTs args + returns response")

        # 4. teaching error: HTTP 500 -> ModelRetry with a useful message
        try:
            by["boom_tool"].func(x="z")
            print("FAIL: expected ModelRetry on HTTP 500"); return 1
        except ModelRetry as e:
            assert "boom_tool" in str(e) or "500" in str(e), str(e)
            print("PASS: HTTP error -> teaching ModelRetry")

        print("\nALL N8N CONTRACT TESTS PASSED")
        return 0
    finally:
        os.unlink(path); srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
