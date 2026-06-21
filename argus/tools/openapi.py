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


def _project(data, fields):
    """Trim a response to an allowlist of top-level keys, so verbose APIs (e.g. a
    Servarr list-* of full objects) don't blow a small-context model. Applies to a
    dict or to each dict in a list; anything else passes through untouched. Keys not
    present are simply absent. `fields` falsy = no projection."""
    if not fields:
        return data
    keys = set(fields)
    if isinstance(data, list):
        return [{k: v for k, v in x.items() if k in keys} if isinstance(x, dict) else x
                for x in data]
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in keys}
    return data


def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]:
    """Provider entry point. The manifest may be a single service (legacy) or hold a
    'services:' list; each service's tool names are prefixed by its name to avoid
    collisions across Servarr apps that share operationIds (e.g. getSystemStatus)."""
    with open(manifest_path, "r") as f:
        manifest = yaml.safe_load(f)
    services = manifest.get("services")
    if services is None:
        return _service_tools(manifest)            # legacy single-service (no prefix)
    out: list[Tool] = []
    for svc in services:
        prefix = f"{svc['name']}_" if svc.get("name") else ""
        out.extend(_service_tools(svc, prefix))
    return out


def _service_tools(service: dict, prefix: str = "") -> list[Tool]:
    """Build tools for one OpenAPI service definition (one spec + base_url + ops)."""
    with open(service["spec"], "r") as f:
        spec = yaml.safe_load(f)

    base_url = service["base_url"].rstrip("/")
    default_timeout = service.get("default_timeout", 30)
    headers = service.get("headers", {})
    allowed_operations = service.get("operations", [])
    tags = service.get("tags", [])
    fields_map = service.get("fields", {})        # operationId -> [keys] response projection
    limits_map = service.get("limits", {})        # operationId -> max list items returned

    def make_dispatch(operation_id, method, path_template, path_params, query_params, body_params, fields, limit):
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
                if "application/json" not in content_type:
                    return resp.text
                data = resp.json()
                if limit and isinstance(data, list):   # cap noisy list endpoints (e.g. search)
                    data = data[:limit]
                return _project(data, fields)
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
        dispatch_func = make_dispatch(operation_id, method.upper(), path_template, path_params, query_params, body_params, fields_map.get(operation_id), limits_map.get(operation_id))
        
        tools_list.append(
            Tool(
                name=prefix + operation_id,
                description=description,
                tags=tags,
                func=dispatch_func,
                provider="openapi",
                schema=schema,
            )
        )
    
    return tools_list
