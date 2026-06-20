"""Loop driver — the harness's orchestration over the Pydantic AI engine.

It selects tools for the task, builds the agent, enforces the turn budget, and
records turn metrics. Pydantic AI is DRIVEN here; it is not the harness. If we
ever swap the engine, only this module changes.
"""
from __future__ import annotations
import time

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from . import metrics, watchdog
from .registry import Registry

SYSTEM_PROMPT = (
    "You are Argus, a homelab assistant. Use the provided tools when they help. "
    "Prefer a tool call over guessing. If a tool returns an error message, read it "
    "and correct your next call."
)


def make_model(model_name: str = "local",
               base_url: str = "http://localhost:4000/v1") -> OpenAIChatModel:
    """Point the engine at the LiteLLM proxy. Model-agnostic via model_name."""
    return OpenAIChatModel(
        model_name, provider=OpenAIProvider(base_url=base_url, api_key="none")
    )


def run(registry: Registry, prompt: str, *, model_name: str = "local",
        base_url: str = "http://localhost:4000/v1", turn_budget: int = 8) -> str:
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
            prompt, usage_limits=UsageLimits(request_limit=turn_budget)
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
