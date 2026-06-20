# n8n Provider — build spec

Implement **`argus/tools/n8n.py`**: expose n8n workflows (webhook-triggered) as Argus tools.
This is tool lane (1). **MIRROR the structure of `argus/tools/native.py`.**

## Hard requirement
Your implementation MUST make `tests/test_n8n_provider.py` pass. Create ONLY the file
`argus/tools/n8n.py` — do not modify any other file.

## Public interface (exactly this)
```python
def tools(manifest_path: str = "config/n8n_tools.yaml") -> list[Tool]: ...
```
- `Tool` is `from ..registry import Tool`.
- Returns one `registry.Tool` per entry in the manifest's `tools:` list.

## Manifest format (YAML)
```yaml
base_url: http://nyx:5678        # n8n host root (no trailing path)
default_timeout: 30              # seconds; optional, default 30
tools:
  - name: add_todo               # tool name (str)
    description: Add a todo item # model-facing description
    tags: [home, tasks]          # list[str], drives select()
    path: /webhook/add-todo      # appended to base_url
    method: POST                 # optional, default POST
    schema:                      # JSON Schema (object) for the params
      type: object
      properties:
        item: {type: string, description: "the todo text"}
      required: [item]
    example: {item: "buy milk"}  # optional
    auth_header: "Bearer xyz"    # optional; sent as the Authorization header
```

## Each Tool you build
- `name` = entry `name`
- `description` = entry `description`
- `tags` = entry `tags`
- `provider = "n8n"`
- `schema` = entry `schema`  (so the registry uses `Tool.from_schema` — external schema)
- `example` = entry `example` (or `None`)
- `func` = a **dispatch closure** (below)

## Dispatch closure behavior
Signature: `def dispatch(**kwargs)` — `kwargs` ARE the tool arguments.
1. POST to `base_url.rstrip('/') + path` with `json=kwargs`, using `method` (default `POST`),
   `timeout=default_timeout`, and an `Authorization: <auth_header>` header if `auth_header` is set.
2. On success (2xx): return `resp.json()` if the body is JSON, else `resp.text`.
3. On HTTP status >= 400: `raise ModelRetry(f"n8n tool '{name}' returned HTTP {code}: {body[:200]}")`.
4. On timeout / connection error: `raise ModelRetry(f"n8n tool '{name}' failed: {err}")`.

`ModelRetry` is `from pydantic_ai.exceptions import ModelRetry`.
Capture per-tool values (name, url, schema, etc.) correctly in each closure — beware the
classic Python late-binding-in-a-loop bug (bind via default args or a factory function).

## Allowed deps
`httpx` and `yaml` (pyyaml) only — both installed. Target ~60–90 lines, in `native.py` style.
