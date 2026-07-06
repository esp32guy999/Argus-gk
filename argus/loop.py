"""Loop driver — the harness's orchestration over the Pydantic AI engine.

It selects tools for the task, builds the agent, enforces the turn budget, and
records turn metrics. Pydantic AI is DRIVEN here; it is not the harness. If we
ever swap the engine, only this module changes.
"""
from __future__ import annotations
import json
import re
import time

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from . import metrics, watchdog
from .registry import Registry

SYSTEM_PROMPT = (
    "You are Argus, a homelab assistant. You run SYNCHRONOUSLY: everything you do "
    "happens inside this single response. You have NO ability to work in the "
    "background, continue after you stop, or get back to the user later. When you "
    "stop writing, the task is over.\n"
    "- If a task needs a tool, CALL THE TOOL NOW. Never announce that you will "
    "('let me look that up', \"I'll check\", \"working on it\") and then stop — "
    "announcing without calling means the work never happens. Act, don't narrate.\n"
    "- Do everything the task needs in THIS response, using as many tool calls as "
    "required, then give the result.\n"
    "- If you genuinely cannot do it (no suitable tool, missing information), say so "
    "plainly and stop. Do not pretend something is in progress.\n"
    "- For a genuinely long or multi-step job the user wants to walk away from, call "
    "start_background_task to run it detached (the user is notified when it finishes), "
    "then tell them it's started. Use this instead of trying to do a huge job inline.\n"
    "- Prefer a tool call over guessing. Get homelab facts (IPs, ports, paths) from "
    "lookup_memory, never from memory. If a tool errors, read the message and "
    "correct your next call."
)

# Argus's voice lives in soul.md (repo root) — editable persona, separate from the
# operating rules above. Hot-reloaded: edits take effect next turn, no restart needed.
import os
_SOUL_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "soul.md")
_soul_cache: tuple[float, str] = (0.0, "")


def _load_soul() -> str:
    """Return soul.md's text, re-reading only when the file changes. Empty if absent."""
    global _soul_cache
    try:
        mtime = os.path.getmtime(_SOUL_PATH)
    except OSError:
        return ""
    if mtime != _soul_cache[0]:
        try:
            _soul_cache = (mtime, open(_SOUL_PATH, encoding="utf-8").read().strip())
        except OSError:
            return _soul_cache[1]
    return _soul_cache[1]


# Per-model soul overlays live in soul.d/ (repo root). A file soul.d/<key>.md is
# appended to the prompt when <key> is a substring of the serving model id — same
# matching rule as LANE_MODEL_GATES, so soul.d/ornith-35b.md covers the -uncensored,
# -ngram and -mtp arms. Hot-reloaded per file like soul.md.
_SOUL_D = os.path.join(os.path.dirname(__file__), os.pardir, "soul.d")
_overlay_cache: dict[str, tuple[float, str]] = {}


def _load_soul_overlay(model_name: str) -> str:
    """Return the concatenated overlay text for this model (usually one file), or ''."""
    try:
        entries = sorted(os.listdir(_SOUL_D))
    except OSError:
        return ""
    parts = []
    for fn in entries:
        if not fn.endswith(".md"):
            continue
        if fn[:-3] not in (model_name or ""):
            continue
        path = os.path.join(_SOUL_D, fn)
        try:
            mtime = os.path.getmtime(path)
            cached = _overlay_cache.get(path)
            if cached is None or cached[0] != mtime:
                _overlay_cache[path] = (mtime, open(path, encoding="utf-8").read().strip())
            parts.append(_overlay_cache[path][1])
        except OSError:
            continue
    return "\n\n".join(p for p in parts if p)


def _system_prompt(model_name: str = "") -> str:
    """Operating rules + the soul (voice) + any per-model overlay. The soul shapes
    tone only; the rules win on behavior."""
    soul = _load_soul()
    out = SYSTEM_PROMPT + ("\n\n# Your voice\n" + soul if soul else "")
    overlay = _load_soul_overlay(model_name)
    if overlay:
        out += "\n\n# This model\n" + overlay
    return out


# Per-model lane gates — the "configure the harness for each model" edict applied to
# TOOL LANES (cf. VISION_MODELS / CHAT_THINKING in ui/server.py). A powerful lane is
# offered ONLY to models cleared for it; an uncleared model simply doesn't receive that
# lane's tools this turn (the lane stays available to cleared models). A provider absent
# from this map is open to ALL models. Match is by substring, so a served model id that
# embeds the short name still matches. claude-code bypasses this loop (native tools), so
# it's unaffected.
LANE_MODEL_GATES: dict[str, set[str]] = {
    "code_edit": {"qwen3-coder-30b", "qwen3-coder-next", "ornith-35b"},  # surgical source edits → coder-class only
    # ornith-35b: cleared 2026-07-03 — probe produced a byte-perfect SEARCH/REPLACE
    # block (imatrix is coding/debugging-calibrated); substring covers all three arms.
    "shell": {"qwen3-next-80b", "qwen3-coder-30b", "qwen3-coder-next", "ornith-35b",
              "gpt-oss"},  # gpt-oss cleared by owner 2026-07-05 (allowlist now populated)
    # shell gated 2026-07-05: run_command has an EMPTY allowlist (= any binary as
    # shane), and every Argus toolset also carries web_fetch — unrestricted shell +
    # web content + small models is the prompt-injection trifecta. Only models
    # trusted for code edits (+ the 80B daily driver) get a shell.
}


# Models DENIED every gated (system-changing) lane, even when a name substring in
# LANE_MODEL_GATES would otherwise clear them. Deny beats allow. This exists because
# the gate matches by substring: "ornith-35b" clears the trusted official base, but
# that string is ALSO embedded in "ornith-35b-uncensored" (Loki, the abliterated
# finetune) — so without an explicit deny Loki silently inherits shell + code_edit.
# Loki fabricates at the edges (eval 24/27: a hallucinated IP + a falsely-claimed
# action) and Shane doesn't trust it with the system. Kept for chat/creative use
# only — no shell, no source edits. (2026-07-05)
LANE_MODEL_DENY: set[str] = {"ornith-35b-uncensored"}

# ── Per-model lane permissions (config/lane_grants.json, driven by the UI widget) ─
# The high-blast lanes below are toggleable per model from the permissions widget.
# LANE_MODEL_GATES above is the in-code SEED (source of truth when no grants file
# exists). At runtime the widget writes config/lane_grants.json, which OVERRIDES the
# seed for these lanes so clearances change live with NO restart (hot-reloaded like
# soul.md). LANE_MODEL_DENY is enforced regardless of the file — a denied model
# (Loki) can never hold a gated lane, even via a hand-edited grants file.
TOGGLEABLE_LANES: tuple[str, ...] = ("shell", "code_edit")
_LANE_GRANTS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config", "lane_grants.json")
_grants_cache: tuple[float, dict] = (0.0, {})


def _load_lane_grants():
    """Return {lane: {models}} from the grants file, hot-reloaded on change; None if
    the file is absent (→ fall back to the LANE_MODEL_GATES seed)."""
    global _grants_cache
    try:
        mtime = os.path.getmtime(_LANE_GRANTS_PATH)
    except OSError:
        return None
    if mtime != _grants_cache[0]:
        try:
            raw = json.load(open(_LANE_GRANTS_PATH, encoding="utf-8"))
            _grants_cache = (mtime, {ln: set(raw.get(ln, [])) for ln in TOGGLEABLE_LANES})
        except Exception as e:
            print(f"[lane_grants] read failed, using seed defaults: {e}")
            return None
    return _grants_cache[1]


def effective_gates() -> dict:
    """LANE_MODEL_GATES seed overlaid with live grants for the toggleable lanes."""
    grants = _load_lane_grants()
    if grants is None:
        return LANE_MODEL_GATES
    merged = dict(LANE_MODEL_GATES)
    for lane in TOGGLEABLE_LANES:
        merged[lane] = grants.get(lane, set())
    return merged


def save_lane_grants(per_lane: dict) -> dict:
    """Persist per-lane model grants from the widget. Denied models (LANE_MODEL_DENY)
    are stripped no matter what the caller sends. Returns the written mapping."""
    clean = {}
    for lane in TOGGLEABLE_LANES:
        models = per_lane.get(lane, []) or []
        clean[lane] = sorted({m for m in models
                              if not any(d in m for d in LANE_MODEL_DENY)})
    os.makedirs(os.path.dirname(_LANE_GRANTS_PATH), exist_ok=True)
    with open(_LANE_GRANTS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    _load_lane_grants()  # refresh the mtime cache immediately
    return clean


def _gate_tools(selected, model_name: str):
    """Drop tools whose lane the current model isn't cleared for.

    Clearance is effective_gates() (seed overlaid with the live grants file).
    LANE_MODEL_DENY takes precedence: a denied model is refused ALL gated lanes even
    if it substring-matches an allow entry. A lane PRESENT in the gates with an empty
    allow-set is closed to everyone (distinct from a lane that's simply ungated)."""
    gates = effective_gates()
    if not gates:
        return selected
    denied = any(d in (model_name or "") for d in LANE_MODEL_DENY)
    kept = []
    for t in selected:
        prov = getattr(t, "provider", None)
        if prov in gates:
            allow = gates[prov]
            if denied or not any(m in (model_name or "") for m in allow):
                continue
        kept.append(t)
    return kept


# ── anti-stall (2026-07-05) ─────────────────────────────────────────────
# Two local-model failure modes the eval suite measures:
#  1. announce-then-stop — the reply promises an action ("let me check…") but
#     the run made zero tool calls. One corrective retry, then honesty.
#  2. budget exhaustion that discards progress — replaced by a summary built
#     from the tools actually called, so partial work is visible + resumable.

_ANNOUNCE_RX = re.compile(
    r"\b(let me|i[’']ll|i will|i[’']m going to|one (moment|sec\w*)|hold on"
    r"|checking|looking (that|it) up|working on (it|that)|give me a (sec\w*|moment))\b"
    r"[^.!?]{0,80}[.!?…]?\s*$",
    re.IGNORECASE,
)


def _looks_unfinished(text: str, tool_calls: int) -> bool:
    """True when a reply that made NO tool calls ENDS on an announcement —
    the tail anchor keeps poems/explanations that merely contain 'I'll' safe."""
    if tool_calls or not text:
        return False
    return bool(_ANNOUNCE_RX.search(text.strip()[-160:]))


def _nudge_prompt(prompt: str, text: str) -> str:
    return (
        prompt + "\n\n[CORRECTION: your previous reply ended by announcing an action "
        f"instead of performing it (“…{text.strip()[-120:]}”). Nothing runs after "
        "you stop writing. CALL the tools now and deliver the finished result. "
        "If you genuinely cannot, say so plainly.]"
    )


def _budget_summary(turn_budget: int, called: list[str]) -> str:
    steps = " → ".join(dict.fromkeys(called)) if called else "no tool calls completed"
    return (
        f"Stopped at the {turn_budget}-turn budget before finishing. "
        f"Progress so far: {steps}. The task may be partially done — "
        "say “continue” to resume the remaining steps."
    )


def _make_sink(called: list[str], on_event=None):
    """Wrap the caller's on_event so the driver also records which tools ran
    (fuel for the nudge check and the budget summary)."""
    def _sink(phase, detail, step):
        if phase == "tool":
            called.append(detail)
        if on_event:
            try:
                on_event(phase, detail, step)
            except Exception:
                pass
    return _sink


def make_model(model_name: str = "local",
               base_url: str = "http://localhost:4000/v1") -> OpenAIChatModel:
    """Point the engine at the LiteLLM proxy. Model-agnostic via model_name."""
    return OpenAIChatModel(
        model_name, provider=OpenAIProvider(base_url=base_url, api_key="none")
    )


def _thinking_settings(enable_thinking: bool | None):
    """Per-call reasoning toggle for thinking-capable local models (e.g. Qwen3.6).

    None -> unchanged (server default). True/False -> forwarded to the llama.cpp
    server as ``chat_template_kwargs.enable_thinking`` via ``extra_body``. Disabling
    is the snappy path: Qwen3.6 with thinking off answers in ~0.1s instead of burning the
    token budget on chain-of-thought (and, on structured tasks, sometimes never
    emitting an answer at all). Returns None when no override is requested so the
    default request shape is untouched.
    """
    if enable_thinking is None:
        return None
    return OpenAIChatModelSettings(
        extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}}
    )


async def stream_run(registry: Registry, prompt: str, *, model_name: str = "local",
                     base_url: str = "http://localhost:4000/v1", turn_budget: int = 8,
                     message_history=None, on_event=None,
                     enable_thinking: bool | None = None, anti_stall: bool = True):
    """Async generator yielding CUMULATIVE assistant text as it streams.

    Same setup as run() (tool selection + watchdog + turn budget) but uses Pydantic
    AI's run_stream so a server can emit bubble_update events. stream_text() yields
    the full text-so-far each step, matching Forge's {content} contract.
    `message_history` (prior turns) is what gives the model memory across turns.
    Anti-stall: an announce-without-acting reply gets one corrective second pass,
    streamed as a continuation of the same bubble.
    """
    selected = _gate_tools(registry.select(prompt), model_name)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event)
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    final = ""
    try:
        async with agent.run_stream(
            prompt, message_history=message_history,
            usage_limits=limits, model_settings=settings,
        ) as result:
            async for text in result.stream_text():   # cumulative text-so-far
                final = text
                yield text
        if anti_stall and _looks_unfinished(final, len(called)):
            metrics.ANNOUNCE_NUDGES.inc()
            sink("nudge", "announce-without-acting", 0)
            async with agent.run_stream(
                _nudge_prompt(prompt, final), message_history=message_history,
                usage_limits=limits, model_settings=settings,
            ) as result2:
                async for text in result2.stream_text():
                    yield f"{final}\n\n{text}"
        metrics.AGENT_TURNS.labels("ok").inc()
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        yield _budget_summary(turn_budget, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


def run(registry: Registry, prompt: str, *, model_name: str = "local",
        base_url: str = "http://localhost:4000/v1", turn_budget: int = 8,
        message_history=None, enable_thinking: bool | None = None,
        on_event=None, anti_stall: bool = True) -> str:
    selected = _gate_tools(registry.select(prompt), model_name)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event)
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    try:
        result = agent.run_sync(prompt, message_history=message_history,
                                usage_limits=limits, model_settings=settings)
        output = result.output
        if anti_stall and _looks_unfinished(output, len(called)):
            metrics.ANNOUNCE_NUDGES.inc()
            sink("nudge", "announce-without-acting", 0)
            result = agent.run_sync(_nudge_prompt(prompt, output),
                                    message_history=message_history,
                                    usage_limits=limits, model_settings=settings)
            output = result.output
        metrics.AGENT_TURNS.labels("ok").inc()
        return output
    except UsageLimitExceeded:                        # turn budget hit -> report progress
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        return _budget_summary(turn_budget, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


async def run_async(registry: Registry, prompt: str, *, model_name: str = "local",
                    base_url: str = "http://localhost:4000/v1", turn_budget: int = 12,
                    message_history=None, on_event=None,
                    enable_thinking: bool | None = None, anti_stall: bool = True) -> str:
    """Non-streaming async run — for the background task runner. Same tool selection
    + watchdog + budget as run(), returns the final text. Higher default budget since
    background jobs are expected to be multi-step."""
    selected = _gate_tools(registry.select(prompt), model_name)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event)
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    try:
        result = await agent.run(prompt, message_history=message_history,
                                 usage_limits=limits, model_settings=settings)
        output = result.output
        if anti_stall and _looks_unfinished(output, len(called)):
            metrics.ANNOUNCE_NUDGES.inc()
            sink("nudge", "announce-without-acting", 0)
            result = await agent.run(_nudge_prompt(prompt, output),
                                     message_history=message_history,
                                     usage_limits=limits, model_settings=settings)
            output = result.output
        metrics.AGENT_TURNS.labels("ok").inc()
        return output
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        return _budget_summary(turn_budget, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)
