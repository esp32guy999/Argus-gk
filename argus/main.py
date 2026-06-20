"""Argus entry point (walking skeleton): one prompt -> answer, tools wired,
metrics live.  Run:  python -m argus.main "what time is it?"
"""
from __future__ import annotations
import sys

from . import metrics
from .registry import Registry
from .tools import native


def build_registry() -> Registry:
    reg = Registry()
    reg.add_provider(native.tools())   # lane 4; n8n / MCP / OpenAPI lanes added next
    return reg


def main() -> None:
    metrics.serve(9101)                # harness sensors on :9101/metrics
    from . import loop                 # import after metrics server is up
    reg = build_registry()
    prompt = " ".join(sys.argv[1:]) or "What time is it, and what is 3*(4+1)?"
    print(loop.run(reg, prompt))


if __name__ == "__main__":
    main()
