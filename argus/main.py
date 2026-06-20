"""Argus entry point (walking skeleton): one prompt -> answer, tools wired,
metrics live.  Run:  python -m argus.main "what time is it?"
"""
from __future__ import annotations
import sys

from . import metrics
from .registry import Registry
from .tools import native


def build_registry() -> Registry:
    """Native lane is always on; the n8n / MCP / OpenAPI lanes load only if their
    manifest exists, and a broken one warns + is skipped rather than crashing."""
    import os
    reg = Registry()
    reg.add_provider(native.tools())                       # lane 4 — always on

    lanes = []
    if os.path.exists("config/n8n_tools.yaml"):
        from .tools import n8n
        lanes.append(("n8n", "config/n8n_tools.yaml", n8n.tools))
    if os.path.exists("config/mcp_servers.yaml"):
        from .tools import mcp_lane
        lanes.append(("mcp", "config/mcp_servers.yaml", mcp_lane.tools))
    if os.path.exists("config/openapi.yaml"):
        from .tools import openapi
        lanes.append(("openapi", "config/openapi.yaml", openapi.tools))

    for name, path, load in lanes:
        try:
            tools = load(path)
            reg.add_provider(tools)
            print(f"[argus] {name}: +{len(tools)} tools from {path}")
        except Exception as e:
            print(f"[argus] WARNING: {name} lane failed ({path}): {e}")

    # Attach semantic select() over everything loaded (lazy-embeds on first use;
    # falls back to all tools if the embeddings server is unreachable).
    try:
        from .semantic import SemanticSelector
        reg.semantic = SemanticSelector(reg.all())
    except Exception as e:
        print(f"[argus] WARNING: semantic selector unavailable: {e}")
    return reg


def main() -> None:
    metrics.serve(9101)                # harness sensors on :9101/metrics
    from . import loop                 # import after metrics server is up
    reg = build_registry()
    prompt = " ".join(sys.argv[1:]) or "What time is it, and what is 3*(4+1)?"
    print(loop.run(reg, prompt))


if __name__ == "__main__":
    main()
