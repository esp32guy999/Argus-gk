"""Prometheus sensors — the harness layer of 'everything gets a sensor'.

Instrumented once at the boundary (here + registry.py), not per-function.
LiteLLM emits the model-layer sensors on its own /metrics; this covers the
harness: tool dispatches, agent turns, and watchdog control events.
"""
from prometheus_client import Counter, Histogram, start_http_server

# --- tool lane ---
TOOL_CALLS = Counter(
    "argus_tool_calls_total", "Tool dispatches", ["provider", "tool", "outcome"]
)
TOOL_LATENCY = Histogram(
    "argus_tool_latency_seconds", "Tool dispatch latency", ["provider", "tool"]
)
TOOL_ERRORS = Counter(
    "argus_tool_errors_total", "Tool errors", ["provider", "tool", "error_type"]
)
TOOL_RETRY = Counter("argus_tool_retry_total", "Teaching-retries raised", ["tool"])

# --- agent / control lane ---
AGENT_TURNS = Counter("argus_agent_turns_total", "Agent task outcomes", ["outcome"])
RESEARCH_ISOLATION_DROPS = Counter(
    "argus_research_isolation_drops_total",
    "Actionable tools withheld because the web/research lane was active", ["model"])

# Memory fact lifecycle (Task 3). Observability so promotion/consolidation thresholds
# can be TUNED FROM DATA (specs/memory_system.md §11), not guessed up front.
MEMORY_TRANSITIONS = Counter(
    "argus_memory_fact_transitions_total", "Fact state transitions", ["to_state"])
MEMORY_FALSE_CONFIRMATIONS = Counter(
    "argus_memory_false_confirmations_total",
    "Facts later found wrong after being confirmed (tune promotion criteria)")
MEMORY_FALSE_INVALIDATIONS = Counter(
    "argus_memory_false_invalidations_total",
    "Facts wrongly invalidated then restored (tune invalidation criteria)")
LOOP_DETECTED = Counter("argus_agent_loop_detected_total", "Repeated-call interventions")
NO_PROGRESS = Counter("argus_agent_no_progress_total", "No-progress give-ups")
ANNOUNCE_NUDGES = Counter(
    "argus_agent_announce_nudges_total",
    "Announce-without-acting corrections (reply promised action, called no tool)")
TASK_DURATION = Histogram("argus_agent_task_duration_seconds", "Task wall-clock seconds")
TOOLS_SELECTED = Histogram(
    "argus_tools_selected_per_turn", "Tools exposed to the model per task",
    buckets=(1, 2, 5, 10, 20, 40, 80),
)

# --- Claude Code lane (bypasses the loop/registry, so it needs its own sensors) ---
CC_TURNS = Counter("argus_claude_code_turns_total", "Claude Code turns", ["outcome"])
CC_DURATION = Histogram("argus_claude_code_turn_seconds", "Claude Code turn wall-clock seconds")


def serve(port: int = 9101) -> None:
    """Start the harness metrics endpoint (separate from LiteLLM's /metrics)."""
    start_http_server(port)
