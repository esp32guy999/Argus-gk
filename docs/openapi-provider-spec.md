# OpenAPI Provider — build spec

Implement **`argus/tools/openapi.py`**: turn selected operations of an OpenAPI spec
into Argus tools. This is tool lane (3). **MIRROR the structure of `argus/tools/n8n.py`.**

## Hard requirement
Your implementation MUST make `tests/test_openapi_provider.py` pass. Create ONLY the
file `argus/tools/openapi.py` — do not modify any other file.

## Public interface (exactly this)
```python
def tools(manifest_path: str = "config/openapi.yaml") -> list[Tool]: ...
```
`Tool` is `from ..registry import Tool`. Returns one `registry.Tool` per allowlisted operation.

## Manifest format (YAML)
```yaml
spec: /path/to/openapi.json      # local path to the OpenAPI spec (JSON or YAML)
base_url: http://glassgarden:7878
headers: {X-Api-Key: "abc"}      # optional; sent on EVERY request
operations: [getMovie, addMovie] # operationId ALLOWLIST — only these become tools (curate!)
tags: [media]
default_timeout: 30              # optional, default 30
```
Load both the manifest and the spec with `yaml.safe_load` (it parses JSON too).

## Building each tool (one per operationId in `operations`)
Find the operation: scan `spec["paths"][<path>][<method>]` for the dict whose
`operationId` matches. Capture the **path template** (e.g. `/movie/{id}`) and the
**method** (`get`/`post`/...).

- `name` = operationId
- `description` = operation `summary` or `description` or the operationId
- `provider` = `"openapi"`, `tags` = manifest tags
- `schema` = a JSON-Schema object built from the operation's inputs:
  - `properties`: each `parameters` entry with `in` in (`path`,`query`) → `{name: param["schema"]}`
    (carry over the param's `description` if present); PLUS, if there is a
    `requestBody`, merge in `content["application/json"]["schema"]["properties"]`.
  - `required`: names of path/query params with `required: true` + the requestBody
    json schema's `required` list.
  - shape: `{"type": "object", "properties": {...}, "required": [...]}`

## Dispatch closure  `def dispatch(**kwargs)`
Capture per-operation: `method`, path template, the set of **path** param names, the
set of **query** param names, the set of **body** property names, `base_url`, `headers`, timeout.
1. `url = base_url.rstrip('/') + path_template`, replacing each `{p}` with `str(kwargs[p])` for path params.
2. `query = {q: kwargs[q] for q in query_names if q in kwargs}`
3. `body = {b: kwargs[b] for b in body_names if b in kwargs}` (or `None` if empty)
4. `resp = httpx.request(method, url, params=query or None, json=body or None, headers=headers, timeout=timeout)`
5. 2xx → `resp.json()` if JSON content-type else `resp.text`.
6. status >= 400 → `raise ModelRetry(f"openapi tool '{name}' returned HTTP {resp.status_code}: {resp.text[:200]}")`
7. timeout/connection error → `raise ModelRetry(f"openapi tool '{name}' failed: {err}")`

`ModelRetry` is `from pydantic_ai.exceptions import ModelRetry`. Beware the
late-binding-in-a-loop closure bug — bind per-operation values via a factory function.

## Allowed deps
`httpx`, `yaml`, `json` only. Target ~80–110 lines, `n8n.py` style.
