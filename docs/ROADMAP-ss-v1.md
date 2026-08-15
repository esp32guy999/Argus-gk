# SUPER-SEE v1 — road to deployed

> **Status:** Phase 0–1 done (2026-08-15). Code + tests committed on
> `feature/ss-v1-shadow`. Next is Phase 2 D0 restart (Shane-approved).
> **From:** untracked shadow code in `argus/ss/`, mixed into a dirty working tree.
> **To:** shadow SS running inside `argus-ui` on anvil, committed and pushed.
> **Not this destination:** authority. SEE stays the only writer of task state.
>
> **Locked decisions**
> - **Sensors from D0.** Live extras are not optional. Inference-stall and
>   resource-failure must be able to fire on day one.
> - **Model-agnostic worker.** SS supervises whichever model is in the harness
>   seat (local llama-swap, Grok, Claude Code, any `models.yaml` external).
>   The judgment head (Z-Engineer) is a *separate* CPU process; it is not the
>   worker and must never ride llama-swap.

---

## Task Anchor

| | |
|---|---|
| **objective** | Get SUPER-SEE v1 from this working tree to live shadow on anvil. |
| **success_condition** | After `systemctl --user restart argus-ui`, a `/task` run attaches `ss_shadow` to SEE decisions, increments `argus_ss_decisions_total`, appends `ss.jsonl`, and cannot change SEE state. Kill switch `ARGUS_SS=0` turns it off. Code is committed, pushed, tests green. |
| **out_of_scope** | Giving SS authority. UI widget. Serving Z-Engineer via llama-swap. Landing the unrelated dirty diffs (GK timeouts, glassgarden pin, UI heartbeat). Nightly promotion / memory F3+. |

---

## Where we actually are

Phase 0–1 closed 2026-08-15. D0 is not live (no restart).

| Piece | State |
|---|---|
| `argus/ss/` package (protocol, snapshot, rules, model, recorder, observe, sensors) | On `feature/ss-v1-shadow` |
| Shadow hook in `argus/see/api.py` | Committed. Annotates only; `authority=False` |
| `argus_ss_decisions_total` | Committed |
| SEE tests isolated with `ARGUS_SS=0` | Committed |
| `tests/test_ss.py` | Green (bench, authority, non-mutation, kill switch, injected model, recorder) |
| Spec / session log | `specs/ss.md`, `docs/session-2026-08-15.md` |
| Dedicated CPU judgment head | **Not a service.** Still Phase 3. Do not put it on llama-swap |
| Live sensors | Wired in `loop.py` + `ui/server.py`. `observe_task` reads the bus |
| Git | Branch `feature/ss-v1-shadow`. Unrelated GK/UI/semantic diffs still dirty, unstaged |
| Commit / push | Phase 0–1 slice. D0 restart not done |

v1 *design* (already in the code comments):

1. Rules first on facts.
2. Z-Engineer Qwen3-4B (think off) only when `is_suspicious`.
3. `SsDecision.authority` hard-forced `False`.
4. SEE owns transitions. SS is a shadow annotation.

---

## Definition of "deployed"

Three rungs. Climb them in order. Stop after D1 unless Shane opens authority.

| Rung | Name | What is live | Gate to the next rung |
|---|---|---|---|
| **D0** | Rules-only shadow + live sensors | `argus-ui` restarted with `ARGUS_SS=1`, `ARGUS_SS_URL` unset. Harness pulse + backend/GPU probes feed every observe. Rules classify. Judgment head off. | Tests green, commit+push, one `/task` smoke, kill switch verified, no SEE-state mutation, stall/resource cases fire from sensors (not just fixtures). |
| **D1** | Judgment head | Dedicated **CPU** `llama-server` on its own port. `ARGUS_SS_URL` set. Rules still win on facts. | Head stays up without touching GPU/VRAM. Timeout/fallback proven. Disagreement log accumulating. |
| **D2** | Observed | Days of `ss.jsonl` vs SEE actions compared. False-interrupt rate known. | Human call. Not automatic. Authority is a *later* project, not a deploy step. |

D0 is "deployed code." D1 is "v1 as designed." D2 is "we trust the log enough to talk about authority."

---

## The working-tree problem (do this first)

SS is not the only dirty thing. **Do not ship a mixed commit.**

| Keep with SS | Leave alone |
|---|---|
| `argus/ss/**` | `argus/grok_code.py` (GK turn/lock timeouts) |
| `argus/see/api.py` (shadow hook only) | `argus/loop.py` (glassgarden ssh briefing) |
| `argus/metrics.py` (SS counter only) | `argus/semantic.py` + `tests/test_semantic_select.py` (gg pin) |
| `tests/test_see*.py` (`ARGUS_SS=0` isolators) | `ui/server.py`, `ui/static/app.js`, `ui/static/styles.css` (heartbeat / pill) |
| new `tests/test_ss.py`, `specs/ss.md`, session log | `config/tool_grants.json`, `.grok_sessions.json.bak.*` |

**Branch:** `feature/ss-v1-shadow` off `master` (or cherry-pick the SS slice). This is substantial/feature work — Shane merges, per `docs/POLICY-ownership-matrix.md`. Do not land it on `feature/per-tool-permissions`.

Practical sequence:

1. Stash or leave the unrelated diffs.
2. New branch.
3. Add only the SS slice + tests + spec.
4. Commit. Push `origin` (`Argus-gk`) and `argus` (`argus.git`).

---

## Phase 0 — Close the code so it can be tested

**Owner:** pairing session. **Blast radius:** Low. Reversible.

Missing pieces that block a honest "v1 is built":

1. **`tests/test_ss.py`** (standalone script, no pytest — see `docs/CONTRIBUTING.md`).
   Cover, with no network:
   - All six bench cases from `/tmp/ss_bench.py` through `observe()` (rules path).
   - `SsDecision.authority` is always `False`, even if a caller passes `True`.
   - `_ss_shadow` attaches `details["ss_shadow"]` and does not change `SeeTask.current_state`.
   - `ARGUS_SS=0` skips the hook.
   - Model path: inject a fake `http_json`; never hit a real port.
   - Malformed model JSON → fallback `NO_OP` / `UNKNOWN`, not an exception.
   - Recorder writes one JSON line when `ARGUS_SS_LOG` is set.
2. **Check in a short spec** — `specs/ss.md`. Lift the protocol + winner + env vars out of `/tmp`. Delete nothing from SEE; this is an addendum, not a fork.
3. **Sensors — decided: wire from the beginning, model-agnostic.**
   - Process-local generation pulse at the *harness* boundary (`loop.stream_run` /
     `run` / `run_async` **and** `ui/server.py` turn lifecycle). Not llama-swap
     metrics. Grok / Claude Code / externals pulse the same bus.
   - `backend_ok` probes the *current worker's* backend (OpenAI-compat URL, or
     OAuth ready). Never hard-code `:9090`.
   - `gpu_ok` is host nvidia-smi, but only counts as a worker resource when the
     seated model actually uses the 5080. Cloud/OAuth seats report `gpu_ok=True`
     so a GPU blip cannot WAIT a Grok turn.
   - Inference stall requires a real token stream (`model.streamed`). Non-streaming
     `run`/`run_async` stay generating without faking stall.
4. **Do not** add `z-engineer` to the chat selector. It is already wrongly listed (`docs/ISSUES.md`). SS must not make that worse.
5. **Do not** special-case a worker model in SS rules. The seated model is data
   on the snapshot (`model.name`, `system.service`), never a branch.

**Exit:** `PYTHONPATH=. .venv/bin/python tests/test_ss.py` prints PASS. Existing `tests/test_see*.py` still green with `ARGUS_SS=0`.

---

## Phase 1 — Offline proof, then save

**Owner:** pairing. **Blast radius:** Low.

```bash
PYTHONPATH=. .venv/bin/python tests/test_ss.py
PYTHONPATH=. .venv/bin/python tests/test_see.py
PYTHONPATH=. .venv/bin/python tests/test_see_contract.py
PYTHONPATH=. .venv/bin/python tests/test_see_acceptance.py
PYTHONPATH=. .venv/bin/python tests/test_docs.py
```

Pre-commit (`hooks/pre-commit`) will run `test_docs.py` and any `tests/test_<module>.py` matching staged `argus/*.py`. `test_ss.py` is what makes that hook useful for this package.

Commit as one logical unit. Session log `docs/session-2026-08-14.md`. Push both remotes. Dirty-tree hygiene rule: this slice leaves git clean **of SS**. Unrelated dirty files stay unstaged.

**Exit:** `git status` shows no SS-related untracked/modified files. `git log origin/feature/ss-v1-shadow -1` exists.

---

## Phase 2 — D0 deploy (rules-only shadow)

**Owner:** Shane approves the restart. **Blast radius:** Low if the hook stays in its `try/except` and `authority=False`. Medium if we accidentally apply SS actions — that is why Phase 0 tests the non-mutation invariant.

`argus-ui` runs from `/home/shane/argus` already (`WorkingDirectory=/home/shane/argus`). Deploy is a restart, not a copy. After merge (or a deliberate checkout of the feature branch on anvil):

Add a drop-in, do not edit the unit in place (ops rule: wire → verify → remove):

`~/.config/systemd/user/argus-ui.service.d/ss.conf`

```ini
[Service]
Environment=ARGUS_SS=1
# D0: no judgment head
# Environment=ARGUS_SS_URL=
Environment=ARGUS_SS_LOG=/home/shane/argus/data/ss.jsonl
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart argus-ui
systemctl --user is-active argus-ui
curl -sf http://127.0.0.1:8210/metrics | grep argus_ss_decisions
```

**Smoke (one real `/task`, not a unit test):**

1. `/task` a short, harmless goal that will call a tool twice (or drive SEE via `see_*` tools).
2. Confirm the SEE decision payload has `details.ss_shadow` with `authority: false`.
3. `tail -1 /home/shane/argus/data/ss.jsonl` is a new line matching that action.
4. Grafana/Prom on glassgarden already scrapes `anvil:8210/metrics` — `argus_ss_decisions_total` appears without a Prom change.
5. Kill switch: set `ARGUS_SS=0`, restart, repeat one event, confirm no new jsonl line and no `ss_shadow`.

**Rollback:** `ARGUS_SS=0` in the drop-in, `daemon-reload`, restart. Or remove `ss.conf`. The hook is fail-open (`except: pass`) so a bug in SS should not crash SEE.

**Exit:** smoke 1–5 pass. Phone-push is optional and out of scope.

---

## Phase 3 — D1 deploy (CPU judgment head)

**Owner:** Shane. **Blast radius:** Medium for GPU if we get this wrong; Low if we copy `embed-server.service`.

Hard rule, already written in `argus/ss/model.py`: **do not put the SS model on llama-swap.** llama-swap holds one GPU resident. Loading Z-Engineer there evicts `qwen3-next-80b`. Z-Engineer is already a llama-swap entry in `deploy/llama-swap-config.yaml` for image work — SS must not use `:9090`.

Copy the embed-server pattern (CPU, `CUDA_VISIBLE_DEVICES=`):

`~/.config/systemd/user/ss-supervisor.service`

```ini
[Unit]
Description=SUPER-SEE judgment head (Z-Engineer Qwen3-4B, CPU, think off)
After=network.target

[Service]
Environment=CUDA_VISIBLE_DEVICES=
ExecStart=/home/shane/llama.cpp/build/bin/llama-server \
  -m /home/shane/models/z-engineer/Qwen3-4b-Z-Image-Engineer-V4-Q5_K_M.gguf \
  -ngl 0 --host 127.0.0.1 --port 8766 -c 4096 --jinja
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Then in `ss.conf`:

```ini
Environment=ARGUS_SS_URL=http://127.0.0.1:8766
Environment=ARGUS_SS_MODEL_TIMEOUT=5
```

Bind **localhost only**. This is not a chat model.

Prove, in order:

1. `curl -sf http://127.0.0.1:8766/health` (or `/v1/models`) while 80B is still the llama-swap resident.
2. `nvidia-smi` — SS process is not on the 5080.
3. Force a *suspicious* snapshot (tool errors, or 2 repeats, or long task / no progress) and see `source=model` in jsonl.
4. Kill `ss-supervisor` and send another suspicious snapshot — observe fallback (`source=fallback` or rules), SEE still healthy, latency ≤ timeout.
5. Confirm a *productive* snapshot still comes from `source=rules` and is `NO_OP`. The model must not be asked.

**Exit:** D0 smoke still passes, plus 1–5 above.

---

## Phase 4 — D2 observe (required before anyone talks authority)

**Owner:** Shane, days not hours. **Blast radius:** none — still shadow.

Read `data/ss.jsonl` against SEE's own action on the same event.

| Signal | How | Fail if |
|---|---|---|
| Coverage | jsonl grows on every `/task` tool event | silent `except: pass` eating all observes |
| Rules vs model mix | `source` label on the Prom counter | model called on every event (rules short-circuit broken) |
| False interrupt | SS `REPLAN`/`ABORT`/`ASK_USER` while SEE stayed `NO_OP`/`CONTINUE` and the task later COMPLETED | more than a rare disagreement on productive work |
| Missed stall | SEE `STALLED` and SS said `NO_OP` | expected until sensors land; do not "fix" by giving authority |
| Cost | `latency_ms` on model lines | p95 ≫ 5s, or GPU blip |

Do **not** flip `authority` in this phase. The mapping `SS_TO_STP` exists so a later phase can apply a decision; it is dead code on purpose.

Suggested window: a week of real `/task` use, or 50 observed events, whichever is more honest.

---

## Later (explicitly not this roadmap)

- **Authority.** Would apply `SS_TO_STP` onto `engine.supervisor_action`. Needs a written disagreement budget, a Shane-facing override, and a separate Task Anchor.
- **Live sensors** for inference stall / GPU / backend (unless chosen in Phase 0).
- **UI** showing `ss_shadow` next to SEE.
- Removing `z-engineer` from the chat selector (`docs/ISSUES.md`).
- Promoting `/tmp/ss_bench.py` into `evals/` as a standing bake-off.

---

## Env reference (v1)

| Var | Default | Meaning |
|---|---|---|
| `ARGUS_SS` | `1` | Master kill switch. `0`/`off`/`false` skips the hook. |
| `ARGUS_SS_URL` | unset | OpenAI-compat base for the CPU head. Unset = rules-only. |
| `ARGUS_SS_MODEL_TIMEOUT` | `5` | Seconds. Failure → fallback, never a SEE crash. |
| `ARGUS_SS_LOG` | next to `ARGUS_DB`, else off | Append-only observe log. |
| `ARGUS_SS_REPEAT_N` | `3` | Identical tool+args → `REPLAN`. |
| `ARGUS_SS_STALL_SEC` | `20` | `seconds_since_token` → `WAIT` (needs sensors). |
| `ARGUS_SS_HIGH_CALLS` | `8` | Busy + no new info → `REPLAN`. |
| `ARGUS_SS_LONG_SEC` | `120` | Suspicious (model may be asked). |
| `ARGUS_SS_SOFT_STALL_SEC` | `5` | Suspicious stall band. |

---

## Risks (and the counter)

| Risk | Why it's real | Counter |
|---|---|---|
| Mixed commit ships GK/UI/semantic with SS | Same dirty tree | Phase "working-tree" is mandatory |
| SS exception takes down a SEE tick | Hook is on the live path | Keep the `try/except`; tests must still prove non-mutation |
| Judgment head evicts the 80B | Z-Engineer already on llama-swap | Dedicated CPU unit, localhost, `CUDA_VISIBLE_DEVICES=` |
| "Deployed" claimed while sensors are dark | Stall/resource rules never fire | Call D0 what it is; don't score those cases |
| Authority creeps in via a flag | `SS_TO_STP` is already written | `authority = False` in `__post_init__`; no env to turn it on |
| Unpushed branch | 2026-07-03 audit | Push both remotes before calling D0 done |

---

## Suggested session cuts

Each cut is one pairing session with a testable exit.

1. Isolate branch + `test_ss.py` + spec (Phase 0–1, no restart).
2. D0 restart + smoke + kill switch (Phase 2).
3. `ss-supervisor.service` + D1 proof (Phase 3), only after D0 has been quiet for a day.

Do not combine 2 and 3. A bad CPU server and a new hook in the same restart hides which one broke chat.
