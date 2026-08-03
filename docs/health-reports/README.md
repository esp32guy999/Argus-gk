# Homelab health reports

JSON probe results from `scripts/homelab_health.py`. Timestamped files are
local/ops artifacts (gitignored); re-run the script to refresh.

```bash
cd ~/argus
PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile core
PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile full
PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile media --see
PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile core --fail-notify
```

Config: `config/homelab_health.yaml` (no secrets — HTTP surface only).
SEE path creates a supervised task, attaches `tool:homelab_health` evidence per
service, marks checklist items, and calls `request_verify`.
