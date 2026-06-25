"""Loop driver — the harness's orchestration over the Pydantic AI engine.

It selects tools for the task, builds the agent, enforces the turn budget, and
records turn metrics. Pydantic AI is DRIVEN here; it is not the harness. If we
ever swap the engine, only this module changes.
"""
from __future__ import annotations
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
                     enable_thinking: bool | None = None):
    """Async generator yielding CUMULATIVE assistant text as it streams.

    Same setup as run() (tool selection + watchdog + turn budget) but uses Pydantic
    AI's run_stream so a server can emit bubble_update events. stream_text() yields
    the full text-so-far each step, matching Forge's {content} contract.
    `message_history` (prior turns) is what gives the model memory across turns.
    """
    selected = registry.select(prompt)
    metrics.TOOLS_SELECTED.observe(len(selected))
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=SYSTEM_PROMPT,
        capabilities=[watchdog.make_capability(on_event=on_event)],
    )
    start = time.perf_counter()
    try:
        async with agent.run_stream(
            prompt, message_history=message_history,
            usage_limits=UsageLimits(request_limit=turn_budget),
            model_settings=_thinking_settings(enable_thinking),
        ) as result:
            async for text in result.stream_text():   # cumulative text-so-far
                yield text
        metrics.AGENT_TURNS.labels("ok").inc()
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        yield (f"Stopped after the {turn_budget}-turn budget without finishing — "
               "avoiding a stall.")
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


def run(registry: Registry, prompt: str, *, model_name: str = "local",
        base_url: str = "http://localhost:4000/v1", turn_budget: int = 8,
        message_history=None, enable_thinking: bool | None = None) -> str:
    selected = registry.select(prompt)
    metrics.TOOLS_SELECTED.observe(len(selected))
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=SYSTEM_PROMPT,
        capabilities=[watchdog.make_capability()],   # anti-stall: repeated-call detection
    )
    start = time.perf_counter()
    try:
        result = agent.run_sync(
            prompt, message_history=message_history,
            usage_limits=UsageLimits(request_limit=turn_budget),
            model_settings=_thinking_settings(enable_thinking),
        )
        metrics.AGENT_TURNS.labels("ok").inc()
        return result.output
    except UsageLimitExceeded:                        # turn budget hit -> give up gracefully
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        return (f"Stopped after the {turn_budget}-turn budget without finishing — "
                "avoiding a stall. Try rephrasing or narrowing the request.")
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


async def run_async(registry: Registry, prompt: str, *, model_name: str = "local",
                    base_url: str = "http://localhost:4000/v1", turn_budget: int = 12,
                    message_history=None, on_event=None,
                    enable_thinking: bool | None = None) -> str:
    """Non-streaming async run — for the background task runner. Same tool selection
    + watchdog + budget as run(), returns the final text. Higher default budget since
    background jobs are expected to be multi-step."""
    selected = registry.select(prompt)
    metrics.TOOLS_SELECTED.observe(len(selected))
    agent = Agent(
        make_model(model_name, base_url),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=SYSTEM_PROMPT,
        capabilities=[watchdog.make_capability(on_event=on_event)],
    )
    start = time.perf_counter()
    try:
        result = await agent.run(
            prompt, message_history=message_history,
            usage_limits=UsageLimits(request_limit=turn_budget),
            model_settings=_thinking_settings(enable_thinking),
        )
        metrics.AGENT_TURNS.labels("ok").inc()
        return result.output
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        return f"Stopped after the {turn_budget}-turn budget without finishing."
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)
