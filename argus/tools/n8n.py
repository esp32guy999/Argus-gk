"""n8n tool lane — webhook-triggered external workflows.

Each tool is a webhook call to an n8n instance. Raise ModelRetry with a corrective
message to TEACH the model on failure instead of returning an opaque error.
"""
from __future__ import annotations

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool


def tools(manifest_path: str = "config/n8n_tools.yaml") -> list[Tool]:
    """Provider entry point: emit n8n tools in the uniform contract."""
    with open(manifest_path, "r") as f:
        config = yaml.safe_load(f)

    base_url = config["base_url"].rstrip("/")
    default_timeout = config.get("default_timeout", 30)
    tools_config = config.get("tools", [])

    def make_dispatch(tool_config):
        name = tool_config["name"]
        url = base_url + tool_config["path"]
        method = tool_config.get("method", "POST").upper()
        schema = tool_config["schema"]
        auth_header = tool_config.get("auth_header")
        example = tool_config.get("example")

        def dispatch(**kwargs):
            headers = {}
            if auth_header:
                headers["Authorization"] = auth_header
            try:
                resp = httpx.request(
                    method, url, json=kwargs, timeout=default_timeout, headers=headers
                )
                if resp.status_code >= 400:
                    raise ModelRetry(f"n8n tool '{name}' returned HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            except (httpx.TimeoutException, httpx.RequestError) as err:
                raise ModelRetry(f"n8n tool '{name}' failed: {err}")

        return dispatch

    return [
        Tool(
            name=tool["name"],
            description=tool["description"],
            tags=tool.get("tags", []),
            func=make_dispatch(tool),
            provider="n8n",
            example=tool.get("example"),
            schema=tool["schema"],
        )
        for tool in tools_config
    ]
