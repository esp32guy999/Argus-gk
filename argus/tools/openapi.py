"""OpenAPI tool lane — generate tools from OpenAPI spec operations.

Each tool is an HTTP call to an API endpoint defined in an OpenAPI spec.
Raise ModelRetry with a corrective message to TEACH the model on failure instead
of returning an opaque error.
"""
from __future__ import annotations

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]:
    """Provider entry point: emit OpenAPI tools in the uniform contract."""
    with open(manifest_path, "r") as f:
        manifest = yaml.safe_load(f)

    with open(manifest["spec"], "r") as f:
        spec = yaml.safe_load(f)

    base_url = manifest["base_url"].rstrip("/")
    default_timeout = manifest.get("default_timeout", 30)
    headers = manifest.get("headers", {})
    allowed_operations = manifest.get("operations", [])
    tags = manifest.get("tags", [])

    def make_dispatch(operation_id, method, path_template, path_params, query_params, body_params):
        def dispatch(**kwargs):
            # Build URL with path parameters
            url = base_url + path_template
            for param in path_params:
                if param not in kwargs:
                    raise ModelRetry(f"Missing required path parameter: {param}")
                url = url.replace(f"{{{param}}}", str(kwargs[param]))
            
            # Build query parameters
            query = {q: kwargs[q] for q in query_params if q in kwargs}
            query = query or None
            
            # Build body parameters
            body = {b: kwargs[b] for b in body_params if b in kwargs}
            body = body or None
            
            try:
                resp = httpx.request(
                    method, url, params=query, json=body, headers=headers, timeout=default_timeout
                )
                if resp.status_code >= 400:
                    raise ModelRetry(f"openapi tool '{operation_id}' returned HTTP {resp.status_code}: {resp.text[:200]}")
                content_type = resp.headers.get("content-type", "")
                return resp.json() if "application/json" in content_type else resp.text
            except (httpx.TimeoutException, httpx.RequestError) as err:
                raise ModelRetry(f"openapi tool '{operation_id}' failed: {err}")

        return dispatch

    tools_list = []
    for operation_id in allowed_operations:
        # Find operation in spec
        method = None
        path_template = None
        operation = None
        for path, methods in spec.get("paths", {}).items():
            for http_method, op in methods.items():
                if http_method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                    continue  # skip path-level "parameters"/"summary"/"$ref" etc.
                if op.get("operationId") == operation_id:
                    method = http_method
                    path_template = path
                    operation = op
                    break
            if method:
                break
        
        if not method:
            continue  # Skip if operation not found
        
        # Extract parameters
        path_params = {p["name"] for p in operation.get("parameters", []) if p["in"] == "path"}
        query_params = {p["name"] for p in operation.get("parameters", []) if p["in"] == "query"}
        body_params = set()
        
        # Extract schema properties from requestBody if present
        schema_properties = {}
        required_properties = list(path_params)
        
        if "requestBody" in operation:
            content = operation["requestBody"].get("content", {})
            if "application/json" in content:
                json_schema = content["application/json"].get("schema", {})
                schema_properties.update(json_schema.get("properties", {}))
                if "required" in json_schema:
                    required_properties.extend(json_schema["required"])
                body_params = set(schema_properties.keys())
        
        # Extract schema properties from path and query parameters
        for param in operation.get("parameters", []):
            if param["in"] in ("path", "query") and "schema" in param:
                prop = param["schema"].copy()
                if "description" in param:
                    prop["description"] = param["description"]
                schema_properties[param["name"]] = prop
                if param.get("required", False):
                    required_properties.append(param["name"])
        
        # Build JSON Schema
        schema = {
            "type": "object",
            "properties": schema_properties,
            "required": list(set(required_properties))  # deduplicate
        }
        
        # Build tool
        description = operation.get("summary", operation.get("description", operation_id))
        dispatch_func = make_dispatch(operation_id, method.upper(), path_template, path_params, query_params, body_params)
        
        tools_list.append(
            Tool(
                name=operation_id,
                description=description,
                tags=tags,
                func=dispatch_func,
                provider="openapi",
                schema=schema,
            )
        )
    
    return tools_list
