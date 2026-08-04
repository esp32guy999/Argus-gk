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
# Flywheel F0–F1: cold-ledger capture quality (importance gate + orchestrator policy)
MEMORY_CANDIDATES = Counter(
    "argus_memory_candidates_total",
    "Cold memory_candidates ledger writes / rejects",
    ["outcome"],  # accepted | rejected | auto_accepted | auto_skipped
)
MEMORY_IMPORTANCE = Histogram(
    "argus_memory_candidate_importance",
    "Importance scores (0-100) for accepted cold candidates",
    buckets=(10, 20, 30, 40, 50, 60, 70, 80, 90, 100),
)
LOOP_DETECTED = Counter("argus_agent_loop_detected_total", "Repeated-call interventions")
NO_PROGRESS = Counter("argus_agent_no_progress_total", "No-progress give-ups")
ANNOUNCE_NUDGES = Counter(
    "argus_agent_announce_nudges_total",
    "Announce-without-acting corrections (reply promised action, called no tool)")
# Turn contract (OpenClaw-inspired): empty is never a valid terminal outcome.
EMPTY_TURNS = Counter(
    "argus_agent_empty_turns_total",
    "Turns with no text and no tool calls (before/without recovery)")
EMPTY_RETRIES = Counter(
    "argus_agent_empty_retries_total",
    "Empty-turn corrective retries",
    ["outcome"],  # recovered | blocked
)
SILENT_REPLIES = Counter(
    "argus_agent_silent_replies_total",
    "Intentional silence (NO_REPLY terminal — no user-visible bubble text)")
TURN_TERMINALS = Counter(
    "argus_agent_turn_terminal_total",
    "Final turn terminal classification after contract resolution",
    ["kind"],  # REPLY | NO_REPLY | TOOL_ONLY | BLOCKED
)
TASK_DURATION = Histogram("argus_agent_task_duration_seconds", "Task wall-clock seconds")
TOOLS_SELECTED = Histogram(
    "argus_tools_selected_per_turn", "Tools exposed to the model per task",
    buckets=(1, 2, 5, 10, 20, 40, 80),
)

# --- Claude Code lane (bypasses the loop/registry, so it needs its own sensors) ---
CC_TURNS = Counter("argus_claude_code_turns_total", "Claude Code turns", ["outcome"])
CC_DURATION = Histogram("argus_claude_code_turn_seconds", "Claude Code turn wall-clock seconds")

# --- Supervisory Execution Engine (SEE) ---
SEE_CREATED = Counter("argus_see_tasks_created_total", "SEE tasks created")
SEE_COMPLETED = Counter("argus_see_tasks_completed_total", "SEE tasks verified complete")
SEE_STALLS = Counter("argus_see_stalls_total", "SEE stall/loop detections")
SEE_EVENTS = Counter("argus_see_events_total", "SEE events ingested", ["type"])
SEE_TASKS = Counter("argus_see_task_state_saves_total", "SEE task saves by state", ["state"])


def serve(port: int = 9101) -> None:
    """Start the harness metrics endpoint (separate from LiteLLM's /metrics)."""
    start_http_server(port)
