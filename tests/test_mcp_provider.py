"""Contract test for the MCP provider (argus/tools/mcp_lane.py).

Spins a REAL in-process MCP server (FastMCP over stdio, as a subprocess) and
connects to it through the lane — exercises the actual MCP handshake + dispatch.
Runnable standalone:  python tests/test_mcp_provider.py   (exit 0 = pass)
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A minimal MCP server used as the test fixture (run as a subprocess over stdio).
FIXTURE = '''\
from mcp.server.fastmcp import FastMCP
m = FastMCP("argus-test")

@m.tool()
def echo(text: str) -> str:
    "Echo the text back."
    return f"echo:{text}"

@m.tool()
def boom() -> str:
    "Always errors."
    raise ValueError("kaboom")

if __name__ == "__main__":
    m.run()
'''


def main() -> int:
    from pydantic_ai.exceptions import ModelRetry

    fd, spath = tempfile.mkstemp(suffix="_mcpserver.py")
    os.write(fd, FIXTURE.encode()); os.close(fd)
    manifest = f"""
servers:
  - name: testsrv
    transport: stdio
    command: {sys.executable}
    args: ["{spath}"]
    tags: [test, mcp]
    timeout: 30
"""
    fd2, mpath = tempfile.mkstemp(suffix=".yaml")
    os.write(fd2, manifest.encode()); os.close(fd2)

    sink = []
    try:
        from argus.tools import mcp_lane
        tools = mcp_lane.tools(mpath, _sink=sink)
        by = {t.name: t for t in tools}

        # 1. MCP server tools -> Tool contract
        assert "echo" in by and "boom" in by, list(by)
        echo = by["echo"]
        assert echo.provider == "mcp", echo.provider
        assert echo.tags == ["test", "mcp"], echo.tags
        assert echo.schema and echo.schema.get("type") == "object", echo.schema
        print("PASS: MCP server -> Tool contract")

        # 2. external-schema path builds a pydantic tool
        for t in tools:
            t.as_pydantic_tool()
        print("PASS: as_pydantic_tool() via from_schema")

        # 3. dispatch round-trips through the live MCP session
        out = echo.func(text="hi")
        assert "echo:hi" in str(out), out
        print("PASS: dispatch round-trips through MCP")

        # 4. tool error -> teaching ModelRetry
        try:
            by["boom"].func()
            print("FAIL: expected ModelRetry on tool error"); return 1
        except ModelRetry as e:
            assert "boom" in str(e), str(e)
            print("PASS: MCP tool error -> teaching ModelRetry")

        print("\nALL MCP CONTRACT TESTS PASSED")
        return 0
    finally:
        for c in sink:
            c.close()
        os.unlink(spath); os.unlink(mpath)


if __name__ == "__main__":
    sys.exit(main())
