"""Contract test for MCP write verification (argus/tools/mcp_lane.py).

A state-changing MCP call that quietly does nothing — or that HA answers with a
"Sorry…" speech string instead of a protocol error — must NOT reach the model as
hollow success. Writes are verified; reads pass through untouched. Offline (fake
MCP result objects, no server). Run:  python tests/test_mcp_verify.py
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic_ai.exceptions import ModelRetry
from argus.tools.mcp_lane import (
    MCPConnection, _classifies_write, _verify_write,
)


class _C:
    def __init__(self, text): self.text = text


class _Result:
    def __init__(self, texts=(), structured=None, isError=False):
        self.content = [_C(t) for t in texts]
        self.structuredContent = structured
        self.isError = isError


class _Session:
    def call_tool(self, name, args): return ("sentinel", name, args)  # _call_async is stubbed


def _conn(spec, result):
    c = MCPConnection.__new__(MCPConnection)
    c.spec = spec
    c.name = spec.get("name", "ha")
    c.timeout = 5
    c._session = _Session()
    c._call_async = lambda coro: result
    return c


def main() -> int:
    # 1. write/read classification
    for w in ("HassTurnOn", "HassTurnOff", "HassLightSet", "HassListRemoveItem",
              "HassSetVolume", "HassMediaPause"):
        assert _classifies_write(w, {}), f"{w} should be a write"
    for r in ("GetLiveContext", "GetDateTime", "todo_get_items"):
        assert not _classifies_write(r, {}), f"{r} should be a read"
    assert _classifies_write("weirdName", {"write_tools": ["weirdName"]})
    assert not _classifies_write("HassTurnOn", {"read_tools": ["HassTurnOn"]})
    print("PASS: write/read classification (+ config overrides)")

    # 2. _verify_write signal handling
    ok, msg, _ = _verify_write("HassTurnOn", "Turned on the switch",
                               {"data": {"success": [{"name": "Porch lights", "id": "switch.porch_lights"}], "failed": []}})
    assert ok and "verified changed: Porch lights" in _  # success enriched with ground truth
    ok, msg, _ = _verify_write("HassTurnOn", "", {"data": {"success": [], "failed": []}})
    assert not ok and "changed nothing" in msg          # empty success -> fail
    ok, msg, _ = _verify_write("HassTurnOn", "Sorry, I am not aware of any area called porch", None)
    assert not ok and "changed nothing" in msg          # failure phrase -> fail
    ok, msg, _ = _verify_write("HassTurnOn", "", {"data": {"code": "no_valid_targets"}})
    assert not ok                                       # error code -> fail
    ok, msg, _ = _verify_write("HassLightSet", "ok", {"data": {"success": [], "failed": [{"id": "light.x"}]}})
    assert not ok and "failed" in msg                   # failed targets -> fail
    ok, msg, out = _verify_write("HassBroadcast", "Message sent", None)
    assert ok and out == "Message sent"                 # unverifiable benign -> passthrough
    # real HA shape: the success/failed block arrives as a JSON *text* string
    ha_ok = '{"speech":{},"response_type":"action_done","data":{"success":[{"name":"Porch lights","id":"switch.porch_lights"}],"failed":[]}}'
    ok, msg, out = _verify_write("HassTurnOn", ha_ok, None)
    assert ok and "verified changed: Porch lights" in out
    ok, msg, _ = _verify_write("HassTurnOn", '{"data":{"success":[],"failed":[]}}', None)
    assert not ok and "changed nothing" in msg          # soft no-op in JSON text -> fail
    print("PASS: _verify_write catches no-op / phrase / code / failed; enriches success (incl JSON-text)")

    # 3. call() end-to-end: write no-op raises, write success enriches, read passes through
    try:
        _conn({}, _Result(texts=["Sorry, I am not aware of any area called porch"])).call("HassTurnOn", {})
        assert False, "no-op write should raise ModelRetry"
    except ModelRetry as e:
        assert "changed nothing" in str(e)

    out = _conn({}, _Result(texts=["Turned on the switch"],
                            structured={"data": {"success": [{"name": "Porch lights"}], "failed": []}})
                ).call("HassTurnOn", {})
    assert "verified changed: Porch lights" in out

    read_out = _conn({}, _Result(texts=["It is 4:05 AM."])).call("GetDateTime", {})
    assert read_out == "It is 4:05 AM."                  # read untouched, no verification

    # 4. opt-out honored
    optout = _conn({"verify_writes": False}, _Result(texts=["Sorry, nothing matched"])).call("HassTurnOn", {})
    assert optout == "Sorry, nothing matched"            # verify disabled -> passthrough
    print("PASS: call() escalates no-op writes, enriches success, leaves reads + opt-out alone")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
