"""Contract test for research-lane isolation (argus/loop.py + tools/web.py).

The load-bearing guarantee: web-derived tools (the research lane) and tools that
ACT on the homelab (shell, code_edit, run_code, fs, media_fs, arr, n8n) are NEVER
offered to the model in the same turn. A prompt-injected web page therefore has no
actionable tool to reach for — the "bubble wrap is around the consequences, not the
content" rule, made structural and testable.

Also pins the precondition that makes gating possible at all: web tools must carry
provider="web" (not the old default "native", which is ungated + always-on).

Offline + deterministic. Runnable standalone:  python tests/test_research_isolation.py
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from argus import loop
    from argus.loop import _gate_tools
    from argus.registry import Tool
    from argus.tools import web

    # Hermetic: ignore any live grants file so we test the in-code seed + isolation,
    # not the operator's current permissions widget state.
    loop._LANE_GRANTS_PATH = os.path.join(tempfile.gettempdir(), "argus_no_such_grants.json")
    loop._grants_cache = (0.0, {})

    def f():
        return "ok"

    # 0. PRECONDITION — web tools must self-identify as the "web" provider.
    web_tools = web.tools()
    for t in web_tools:
        assert t.provider == "web", (
            f"{t.name} has provider={t.provider!r}; must be 'web' or it can't be "
            f"isolated (defaults to ungated 'native')")
    print("PASS: web tools carry provider='web'")

    shell = Tool("run_command", "shell", ["shell"], f, provider="shell")
    edit = Tool("edit_source", "edit", ["code"], f, provider="code_edit")
    runc = Tool("run_python", "run", ["code"], f, provider="run_code")
    native = Tool("get_time", "clock", ["time"], f, provider="native")

    # gemma4-26b is cleared for shell + code_edit in the seed, so gating keeps those
    # actionable tools — meaning anything dropped in the web case is dropped by
    # ISOLATION, not by clearance. Measured relative to each model's own baseline so
    # the test never hardcodes the gate seed.
    def offered(sel, model="gemma4-26b"):
        return {t.name for t in _gate_tools(sel, model)}

    actionable_all = [shell, edit, runc]

    # 2 (baseline first). No web in selection → gating behaves normally; isolation is a
    #    no-op. Whatever the model is cleared for, it keeps.
    base = offered(actionable_all + [native])
    assert "get_time" in base, f"read-only native tool missing from baseline: {base}"
    base_actionable = base & {"run_command", "edit_source", "run_python"}
    assert base_actionable, f"expected gemma to clear some actionable lane: {base}"
    print(f"PASS: no web in selection → actionable lanes unaffected (baseline {sorted(base_actionable)})")

    # 1. Add web tools to the SAME selection → every actionable tool the model would
    #    otherwise have is withheld; web survives; read-only native survives.
    n = offered(web_tools + actionable_all + [native])
    assert "web_search" in n and "web_fetch" in n, f"web tools should survive: {n}"
    assert "get_time" in n, f"read-only native tool wrongly dropped: {n}"
    leaked = n & base_actionable
    assert not leaked, f"actionable tools co-offered with web — isolation breach: {leaked}"
    print("PASS: web present → every actionable lane dropped, web + read-only kept")

    # 3. escape hatch off-switch is honoured (web + shell co-offered again).
    os.environ["ARGUS_RESEARCH_ISOLATION"] = "off"
    try:
        # runc (run_code) is the actionable lane gemma clears in the seed; with the
        # hatch off it must co-exist with web again.
        n = offered(web_tools + [runc])
        assert "run_python" in n and "web_search" in n, \
            f"escape hatch ARGUS_RESEARCH_ISOLATION=off ignored: {n}"
    finally:
        del os.environ["ARGUS_RESEARCH_ISOLATION"]
    print("PASS: ARGUS_RESEARCH_ISOLATION=off escape hatch works")

    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
