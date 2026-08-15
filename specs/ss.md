# SUPER-SEE (SS) v1 — shadow supervisor

> **Status:** Implementing. Follow `docs/ROADMAP-ss-v1.md`.
> **Addendum to** `specs/see.md` / `specs/see-protocol.md`. Not a second supervisor.

SS watches the same SEE task the worker is on and emits a shadow decision.
SEE remains the only writer of task state. `SsDecision.authority` is hard-forced
`False` in v1.

## Model-agnostic worker

The *worker* is whichever model is in the harness seat:

- any llama-swap / OpenAI-compat id
- `grok` (OAuth)
- `claude-code` (OAuth)
- any `models.yaml` `external`

SS rules never branch on a worker model id. The snapshot carries `model.name`
and `system.service` as data.

The optional **judgment head** (Z-Engineer Qwen3-4B, thinking off) is a
*separate* CPU `llama-server`. It is not a chat model and must never ride
llama-swap (that evicts the GPU primary).

## Sensors (live from D0)

`argus/ss/sensors.py` is the bus. `observe_task` always reads it.

| Field | Source |
|---|---|
| `model.generating` / `seconds_since_token` / `streamed` | Process-local pulse: `begin_turn` / `mark_activity` / `end_turn`. Wired in `loop.stream_run` (and run/run_async) **and** `ui/server.py` turn lifecycle so Grok / CC / externals pulse the same bus. |
| `model.streamed` | True only after a real token/activity pulse. Non-streaming `run`/`run_async` stay generating without faking an inference stall. |
| `system.backend_ok` | Probe the *current* worker backend (OpenAI-compat `/models`, or Grok `oauth_ready`). Cached ~2s. Unknown ≠ down. |
| `system.gpu_ok` | Host `nvidia-smi` **only when the seated worker uses the 5080**. OAuth / external seats report `gpu_ok=True`. |

## Decision path

```
snapshot (SEE task + live sensors)
        │
        ▼
   apply_rules   → decision if the fact is unambiguous
        │ None
        ▼
   is_suspicious? ─ no ─► NO_OP / UNKNOWN
        │ yes
        ▼
   Z-Engineer (if ARGUS_SS_URL) or fallback NO_OP
```

Rules win on: backend down, GPU down (local worker), inference stall,
productive progress, repeated tool, busy + no new info.

## Actions

`NO_OP | WAIT | RETRY | REPLAN | VERIFY | ABORT | ASK_USER`

Mapped to STP only if authority is ever turned on (`SS_TO_STP` in
`argus/ss/protocol.py`). v1 never applies the mapping.

## Env

| Var | Default | Meaning |
|---|---|---|
| `ARGUS_SS` | `1` | Master kill switch |
| `ARGUS_SS_URL` | unset | Judgment-head base. Unset = rules-only |
| `ARGUS_SS_MODEL_TIMEOUT` | `5` | Head timeout → fallback |
| `ARGUS_SS_LOG` | next to `ARGUS_DB` | Append-only observe JSONL |
| `ARGUS_SS_REPEAT_N` | `3` | Identical tool+args → REPLAN |
| `ARGUS_SS_STALL_SEC` | `20` | Streamed silence → WAIT |
| `ARGUS_SS_HIGH_CALLS` | `8` | Busy + no info → REPLAN |
| `ARGUS_SS_PROBE_TTL` | `2` | Sensor cache |
| `ARGUS_SS_PROBE_TIMEOUT` | `0.4` | Probe timeout |

## Out of v1

Authority. UI. Serving the judgment head on llama-swap. Worker-model special cases.
