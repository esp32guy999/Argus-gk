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
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

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
    "correct your next call.\n"
    "- glassgarden / gg / Unraid NAS: do NOT use ssh/scp (blocked on purpose). Reach it "
    "via tools: sonarr_*/radarr_*/lidarr_*/prowlarr_* (openapi), radarr_add_movie / "
    "sonarr_add_series (acquire), media_fs on ~/movies and ~/tv (CIFS mounts), "
    "navidrome_*, audiobook_*, media_health_scan, media_remonitor_*. Host aliases: "
    "glassgarden or 192.168.4.206 — HTTP APIs, not shell ssh.\n"
    "- Multi-step work with measurable outcomes: use SEE (see_start_task → tools → "
    "see_checkpoint / see_add_evidence → see_request_verify). Never declare success "
    "yourself on a SEE task; only the supervisor can COMPLETE after evidence. "
    "Hollow claims ('ok', 'done') are rejected. After interruption use see_resume.\n"
    "- Turn contract: every turn MUST end as one of — a real user-visible reply; "
    "tool call(s) that do the work; intentional silence (output ONLY the token "
    "NO_REPLY and nothing else); or a short plain statement that you are blocked. "
    "Empty output (no text and no tools) is never valid — the harness will retry "
    "once, then surface a blocked error."
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


# Friendly labels for each tool lane — the model's "instrument panel" (Move 2).
# provider -> (emoji+label, one-line what-it-does). A provider not listed still shows
# under its raw name, so a newly-added lane is never silently hidden from the pilot.
PROVIDER_BRIEF: dict[str, tuple[str, str]] = {
    "web":         ("🔍 Web", "search the web & fetch pages"),
    "weather":     ("🌤 Weather", "forecasts & conditions"),
    "notes":       ("📝 Notes", "jot & recall notes"),
    "native":      ("🧭 System", "GPU/disk/host stats & Home-Assistant state"),
    "mcp":         ("🏠 Home / MCP", "Home Assistant + connected MCP servers"),
    "navidrome":   ("🎵 Music", "the Navidrome music library"),
    "audiobook":   ("📚 Audiobooks", "the audiobook library"),
    "media_fs":    ("🎞 Media files", "browse/manage the media library"),
    "openapi":     ("🔌 APIs", "glassgarden *arr APIs (no ssh)"),
    "arr_acquire": ("📥 Acquire", "download movies/TV/music/books on glassgarden"),
    "shell":       ("🛠 Shell", "local shell only — no ssh to glassgarden"),
    "code_edit":   ("✏️ Code edit", "surgical source edits"),
    "run_code":    ("🧪 Run code", "execute code in a sandbox"),
    "fs":          ("📂 Filesystem", "read/write/edit files"),
    "see":         ("🎯 SEE", "supervised tasks — start/checkpoint/evidence/verify (no hollow success)"),
}


def _cockpit_briefing(registry, model_name: str) -> str:
    """A generated 'here are your instruments' panel: the lanes THIS model is cleared
    for, plus which powerful lanes are locked behind a grant. Built from the live
    registry + gates so it's always accurate and every model is oriented on load."""
    if registry is None:
        return ""
    gates = effective_gates()
    denied = any(d in (model_name or "") for d in LANE_MODEL_DENY)
    have: set[str] = set()
    locked: set[str] = set()
    for t in registry.all():
        prov = getattr(t, "provider", None) or "native"
        if prov in gates:
            allow = gates[prov]
            if denied or not any(m in (model_name or "") for m in allow):
                locked.add(prov)
                continue
        have.add(prov)
    if not have and not locked:
        return ""
    lines = ["# Your cockpit — instruments you can reach",
             "Call a tool to use its control; if you need one that isn't listed, say so."]
    for prov in sorted(have):
        label, desc = PROVIDER_BRIEF.get(prov, (prov, ""))
        lines.append(f"- {label} — {desc}" if desc else f"- {label}")
    if locked:
        names = ", ".join(PROVIDER_BRIEF.get(p, (p, ""))[0] for p in sorted(locked))
        lines.append(f"Locked (ask Shane to grant via the permissions widget): {names}")
    return "\n".join(lines)


def _system_prompt(model_name: str = "", registry=None, persona: str | None = None) -> str:
    """Operating rules + the selected voice + any per-model overlay + the cockpit
    briefing. Voice is the persona (default Argus/`soul.md`); the rules win on behavior.
    Model overlays in soul.d/ stay capability notes, not names."""
    from . import persona as persona_mod
    soul = persona_mod.voice(persona)
    out = SYSTEM_PROMPT + ("\n\n# Your voice\n" + soul if soul else "")
    overlay = _load_soul_overlay(model_name)
    if overlay:
        out += "\n\n# This model\n" + overlay
    briefing = _cockpit_briefing(registry, model_name)
    if briefing:
        out += "\n\n" + briefing
    return out


# Per-model lane gates — the "configure the harness for each model" edict applied to
# TOOL LANES (cf. vision/thinking in config/models.yaml via model_config). A powerful lane is
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
    "run_code": {"gemma4-26b", "qwen3-next-80b", "qwen3-coder-30b", "qwen3-coder-next"},
    # run_code (EXPERIMENTAL 2026-07-08): write+execute code, but ONLY inside the
    # bubblewrap sandbox (read-only host, NO network, hard timeout) — so it's safe to
    # clear for capable local models incl. gemma (the sandbox is the guardrail, not the
    # gate). Loki stays out via LANE_MODEL_DENY. No export yet (proven code can't leave
    # the ephemeral scratch without a future human-gated promotion step).
    "fs": {"qwen3-next-80b", "qwen3-coder-30b", "qwen3-coder-next"},
    # fs (2026-07-08): the official @modelcontextprotocol/server-filesystem MCP server at
    # SYSTEM-WIDE root (/) — read/write/edit/delete anywhere shane can. Owner-chosen blast
    # radius; still trusted coders + the 80B only by default (NOT gemma; it fabricates),
    # Loki denied via LANE_MODEL_DENY. Widen per-model in the permissions widget.
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
TOGGLEABLE_LANES: tuple[str, ...] = ("shell", "code_edit", "fs")
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
            # only lanes the file actually specifies — an absent toggleable lane (e.g.
            # one added to the code after the file was written) falls back to the SEED,
            # not to "granted to nobody".
            _grants_cache = (mtime, {ln: set(raw[ln]) for ln in TOGGLEABLE_LANES if ln in raw})
        except Exception as e:
            print(f"[lane_grants] read failed, using seed defaults: {e}")
            return None
    return _grants_cache[1]


def effective_gates() -> dict:
    """LANE_MODEL_GATES seed, overlaid with the widget's live grants for the toggleable
    lanes, then unioned with any per-model `grant:` declared in the model manifest
    (config/models.yaml) — so a model can be born cleared for a lane from its manifest
    entry, keeping onboarding in one place. Manifest grants default empty → no-op."""
    grants = _load_lane_grants()
    merged = dict(LANE_MODEL_GATES)
    if grants is not None:
        for lane in TOGGLEABLE_LANES:
            if lane in grants:              # file specifies it (even empty = revoke-all)
                merged[lane] = grants[lane]  # else keep the seed for that lane
    try:
        from . import model_config
        for model_id, lanes in model_config.all_grants().items():
            for lane in lanes:
                merged[lane] = set(merged.get(lane, set())) | {model_id}
    except Exception:
        pass
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


# ── Per-TOOL permissions (config/tool_grants.json, driven by the widget) ──────
# Finer than lanes: an explicit per-model, per-tool on/off override. Absent entry →
# the tool follows its lane default (ungated → on; gated → per effective_gates()).
# LANE_MODEL_DENY still wins on gated lanes (a per-tool grant can't arm Loki's shell).
# CONVENTION: every tool auto-appears in the permissions widget (it lists registry.all()),
# so new tools are toggleable with no widget edit; a tool with no override keeps its lane
# default. New models likewise appear from the manifest — nothing to hand-add.
_TOOL_GRANTS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config", "tool_grants.json")
_tool_grants_cache: tuple[float, dict] = (0.0, {})


def _load_tool_grants() -> dict:
    """{model_substr: {tool_name: bool}} of explicit overrides; {} if no file."""
    global _tool_grants_cache
    try:
        mtime = os.path.getmtime(_TOOL_GRANTS_PATH)
    except OSError:
        return {}
    if mtime != _tool_grants_cache[0]:
        try:
            _tool_grants_cache = (mtime, json.load(open(_TOOL_GRANTS_PATH, encoding="utf-8")))
        except Exception as e:
            print(f"[tool_grants] read failed: {e}")
    return _tool_grants_cache[1]


def _tool_override(model_name: str, tool_name: str):
    """Explicit on/off for this model+tool, or None if unset."""
    for key, tools in _load_tool_grants().items():
        if key in (model_name or "") and tool_name in tools:
            return bool(tools[tool_name])
    return None


def save_tool_grants(grants: dict) -> dict:
    """Persist {model: {tool: bool}} from the widget (hot-reloaded next turn)."""
    os.makedirs(os.path.dirname(_TOOL_GRANTS_PATH), exist_ok=True)
    with open(_TOOL_GRANTS_PATH, "w", encoding="utf-8") as f:
        json.dump(grants, f, indent=2)
    _load_tool_grants()
    return grants


# ── research-lane isolation (2026-07-09) ────────────────────────────────
# The bubble wrap is around the CONSEQUENCES, not the content: a prompt-injected web
# page can only ever produce a wrong summary if it has no actionable tool to reach for.
# So web-derived tools (provider "web") and tools that ACT on the homelab are NEVER
# offered in the same turn. Read-only is the safe default: on a tie, web wins and the
# actionable lanes drop. Tools in neither set (native clock, weather, docs) are always
# kept — isolation only ever removes reach, never read-only capability.
WEB_PROVIDER = "web"
ACTIONABLE_PROVIDERS: frozenset[str] = frozenset({
    "shell", "code_edit", "run_code", "fs", "media_fs",
    "arr_acquire", "lidarr", "n8n", "navidrome", "audiobook", "openapi",
})


# Marker (from tools/web.py provenance envelope) proving web content has entered a
# session. Per-session isolation is stateless: if any prior turn carries this string,
# the session is web-tainted and actionable lanes stay off — no session-id plumbing,
# and a fresh session (empty history) is the reset.
_WEB_TAINT_MARKER = "UNTRUSTED WEB DATA"


def _history_web_tainted(history) -> bool:
    if not history:
        return False
    try:
        blob = str(history)
    except Exception:
        return False
    return _WEB_TAINT_MARKER in blob


def _apply_research_isolation(kept, model_name: str, history=None):
    """Drop every actionable-lane tool when the web/research lane is in play. Trigger
    is per-SESSION: either a web tool survived gating THIS turn, or a prior turn already
    pulled web content into context (the provenance marker is in history). Web wins ties
    (read-only is the safe default). Disable with ARGUS_RESEARCH_ISOLATION=off; reset by
    starting a fresh session (empty history)."""
    if os.environ.get("ARGUS_RESEARCH_ISOLATION", "on").lower() == "off":
        return kept
    web_now = any(getattr(t, "provider", None) == WEB_PROVIDER for t in kept)
    if not web_now and not _history_web_tainted(history):
        return kept  # no web content this turn or earlier this session → untouched
    dropped = [t for t in kept if getattr(t, "provider", None) in ACTIONABLE_PROVIDERS]
    if dropped:
        names = ", ".join(sorted(t.name for t in dropped))
        print(f"[research-isolation] web lane active for {model_name!r}; "
              f"withheld actionable tools: {names}")
        try:
            metrics.RESEARCH_ISOLATION_DROPS.labels(model_name or "").inc(len(dropped))
        except Exception:
            pass  # metric optional; never let observability break the gate
    return [t for t in kept if getattr(t, "provider", None) not in ACTIONABLE_PROVIDERS]


def _gate_tools(selected, model_name: str, history=None):
    """Drop tools whose lane the current model isn't cleared for, then apply
    research-lane isolation (web vs actionable are mutually exclusive, per SESSION).

    Clearance is effective_gates() (seed overlaid with the live grants file).
    LANE_MODEL_DENY takes precedence: a denied model is refused ALL gated lanes even
    if it substring-matches an allow entry. A lane PRESENT in the gates with an empty
    allow-set is closed to everyone (distinct from a lane that's simply ungated).
    `history` (prior turns) lets isolation persist across the session once web content
    has been pulled in — not just the turn it happened."""
    gates = effective_gates()
    if not gates:
        return _apply_research_isolation(list(selected), model_name, history)
    denied = any(d in (model_name or "") for d in LANE_MODEL_DENY)
    kept = []
    for t in selected:
        prov = getattr(t, "provider", None)
        allowed = True
        if prov in gates:                       # lane default
            allow = gates[prov]
            allowed = not denied and any(m in (model_name or "") for m in allow)
        ov = _tool_override(model_name, t.name)  # widget per-tool override wins…
        if ov is not None:
            allowed = ov
        if denied and prov in gates:            # …except Loki's gated-lane floor
            allowed = False
        if allowed:
            kept.append(t)
    return _apply_research_isolation(kept, model_name, history)


# ── anti-stall (2026-07-05) + turn contract (2026-08) ───────────────────
# Local-model failure modes the harness owns (not the model's manners):
#  1. announce-then-stop — promises an action but zero tool calls → one nudge.
#  2. budget exhaustion — summary of tools so far, not a silent drop.
#  3. empty turn — no text and no tools → one empty-retry, then BLOCKED.
#  4. NO_REPLY — intentional silence sentinel; stripped before the UI bubble.
#
# Terminal kinds after resolution: REPLY | NO_REPLY | TOOL_ONLY | BLOCKED.
# EMPTY is never a successful terminal — it is always retried or escalated.

_ANNOUNCE_RX = re.compile(
    r"\b(let me|i[’']ll|i will|i[’']m going to|one (moment|sec\w*)|hold on"
    r"|checking|looking (that|it) up|working on (it|that)|give me a (sec\w*|moment))\b"
    r"[^.!?]{0,80}[.!?…]?\s*$",
    re.IGNORECASE,
)

# Intentional silence (OpenClaw-style silent sentinel). Whole-message only.
NO_REPLY_TOKEN = "NO_REPLY"
_NO_REPLY_EXACT = frozenset({
    "NO_REPLY", "NO_REPLY.", "no_reply", "no_reply.",
    "`NO_REPLY`", "```NO_REPLY```",
})

BLOCKED_EMPTY_MSG = (
    "I got an empty model response (no text and no tools). "
    "Try again, or switch models if it keeps happening."
)


def is_no_reply(text: str | None) -> bool:
    """True when the entire assistant text is the intentional-silence token."""
    if text is None:
        return False
    t = text.strip()
    if not t:
        return False
    if t in _NO_REPLY_EXACT or t.upper().strip("`").rstrip(".") == NO_REPLY_TOKEN:
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) == 1:
        core = lines[0].strip("`").rstrip(".").upper()
        if core == NO_REPLY_TOKEN:
            return True
    return False


def classify_turn(text: str | None, tool_calls: int) -> str:
    """Classify a raw model turn before contract resolution.

    Returns one of: REPLY | NO_REPLY | EMPTY | TOOL_ONLY.
    EMPTY means no text and no tools — invalid terminal until retried.
    """
    n = int(tool_calls or 0)
    t = (text or "").strip()
    if n > 0:
        # Tools ran: silence token alone is still a successful tool-only turn.
        if not t or is_no_reply(t):
            return "TOOL_ONLY"
        return "REPLY"
    if not t:
        return "EMPTY"
    if is_no_reply(t):
        return "NO_REPLY"
    return "REPLY"


def empty_retry_prompt(prompt: str) -> str:
    return (
        prompt + "\n\n[CORRECTION: your previous turn produced no reply and no tool "
        "calls. Empty output is not allowed. CALL tools and answer, reply with the "
        f"result, say plainly that you are blocked, or output only {NO_REPLY_TOKEN} "
        "if intentional silence is correct.]"
    )


def resolve_turn_text(text: str | None, tool_calls: int) -> tuple[str, str]:
    """Map a classified turn to (user_visible_text, terminal_kind).

    NO_REPLY → ("", "NO_REPLY") so the UI can hide the bubble.
    EMPTY is returned as ("", "EMPTY") — caller must retry before accepting.
    TOOL_ONLY with only NO_REPLY body → ("", "TOOL_ONLY").
    """
    kind = classify_turn(text, tool_calls)
    if kind == "EMPTY":
        return "", "EMPTY"
    if kind == "NO_REPLY":
        return "", "NO_REPLY"
    if kind == "TOOL_ONLY":
        t = (text or "").strip()
        if not t or is_no_reply(t):
            return "", "TOOL_ONLY"
        return t, "TOOL_ONLY"
    return (text or "").strip(), "REPLY"


def public_turn_payload(text: str | None, tool_calls: int = 0) -> dict:
    """UI/storage helper: strip NO_REPLY and flag silent turns.

    Does not convert EMPTY→BLOCKED (that is the loop's job after retry).
    """
    visible, kind = resolve_turn_text(text, tool_calls)
    return {
        "text": visible,
        "kind": kind,
        "silent": kind == "NO_REPLY",
        "empty": kind == "EMPTY",
    }


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


def _record_terminal(kind: str) -> None:
    try:
        metrics.TURN_TERMINALS.labels(kind).inc()
        if kind == "NO_REPLY":
            metrics.SILENT_REPLIES.inc()
    except Exception:
        pass


def _apply_empty_and_silent(
    text: str | None,
    tool_calls: int,
    *,
    retried_empty: bool,
) -> tuple[str, str]:
    """After optional empty-retry: return (visible_text, terminal_kind).

    If still EMPTY after a retry was already done, escalate to BLOCKED.
    """
    visible, kind = resolve_turn_text(text, tool_calls)
    if kind == "EMPTY":
        if retried_empty:
            try:
                metrics.EMPTY_RETRIES.labels("blocked").inc()
            except Exception:
                pass
            _record_terminal("BLOCKED")
            return BLOCKED_EMPTY_MSG, "BLOCKED"
        # Caller should retry (and should have counted EMPTY_TURNS at detection).
        return "", "EMPTY"
    if kind == "NO_REPLY":
        _record_terminal("NO_REPLY")
        return "", "NO_REPLY"
    _record_terminal(kind)
    return visible, kind


def _budget_summary(turn_budget: int, called: list[str]) -> str:
    steps = " → ".join(dict.fromkeys(called)) if called else "no tool calls completed"
    return (
        f"Stopped at the {turn_budget}-turn budget before finishing. "
        f"Progress so far: {steps}. The task may be partially done — "
        "say “continue” to resume the remaining steps."
    )


def _tool_retry_summary(exc: BaseException, called: list[str]) -> str:
    """Watchdog ModelRetry can exhaust the tool's max_retries and crash the turn.

    Return a progress sentence instead of a raw exception bubble.
    """
    msg = str(exc)
    m = re.search(r"Tool '([^']+)' exceeded max retries", msg)
    tool = (m.group(1) if m else (called[-1] if called else "a tool"))
    steps = " → ".join(dict.fromkeys(called)) if called else "no tool calls completed"
    return (
        f"Stopped: {tool} was repeated until the harness cut it off. "
        f"Progress so far: {steps}. Change approach rather than retrying the same call."
    )


def _make_sink(called: list[str], on_event=None, *, conversation_id: str | None = None):
    """Wrap the caller's on_event so the driver also records which tools ran
    (fuel for the nudge check and the budget summary). Forwards tool events to
    an active SEE task for the conversation when present."""
    def _sink(phase, detail, step):
        if phase == "tool":
            called.append(detail)
            if conversation_id and detail and not str(detail).startswith("see_"):
                try:
                    from argus.see import api as see_api
                    task = see_api.active_for_conversation(conversation_id)
                    if task:
                        see_api.on_tool(task.id, str(detail), phase="call")
                except Exception:
                    pass
        if phase == "loop" and conversation_id:
            try:
                from argus.see import api as see_api
                from argus.see.models import SeeEvent
                task = see_api.active_for_conversation(conversation_id)
                if task:
                    see_api.on_event(task.id, SeeEvent(
                        type="LoopDetected", detail=str(detail or "tool"),
                    ))
            except Exception:
                pass
        if on_event:
            try:
                on_event(phase, detail, step)
            except Exception:
                pass
    return _sink


def make_model(model_name: str = "local",
               base_url: str = "http://localhost:4000/v1",
               api_key: str = "none") -> OpenAIChatModel:
    """Point the engine at any OpenAI-compat endpoint. Model-agnostic via model_name.

    `api_key` defaults to \"none\" for local servers (llama-swap/ollama) that ignore
    auth; cloud externals (e.g. xAI Grok) pass a real key from model_config.
    """
    return OpenAIChatModel(
        model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key or "none")
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
                     base_url: str = "http://localhost:4000/v1", api_key: str = "none",
                     turn_budget: int = 8,
                     message_history=None, on_event=None,
                     enable_thinking: bool | None = None, anti_stall: bool = True,
                     conversation_id: str | None = None, persona: str | None = None):
    """Async generator yielding CUMULATIVE assistant text as it streams.

    Same setup as run() (tool selection + watchdog + turn budget) but uses Pydantic
    AI's run_stream so a server can emit bubble_update events. stream_text() yields
    the full text-so-far each step, matching Forge's {content} contract.
    `message_history` (prior turns) is what gives the model memory across turns.
    Anti-stall: an announce-without-acting reply gets one corrective second pass,
    streamed as a continuation of the same bubble.
    After a successful turn, orchestrator memory_policy.after_turn may append
    high-importance cold candidates (never auto-confirms facts).
    """
    selected = _gate_tools(registry.select(prompt), model_name, message_history)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event, conversation_id=conversation_id)
    agent = Agent(
        make_model(model_name, base_url, api_key=api_key),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name, registry, persona=persona),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    final = ""
    # If this conversation has an active SEE task, prepend worker brief once.
    see_prefix = ""
    if conversation_id:
        try:
            from argus.see import api as see_api
            t = see_api.active_for_conversation(conversation_id)
            if t:
                see_api.tick(t.id)
                see_prefix = see_api.worker_brief(t.id) + "\n\n"
        except Exception:
            pass
    try:
        async with agent.run_stream(
            (see_prefix + prompt) if see_prefix else prompt,
            message_history=message_history,
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
                # Pass-2 stream_text is cumulative for the *nudge* only. Do not
                # fold cumulative snapshots as if they were new paragraphs, and
                # skip if the model just re-emitted the same answer (echo).
                first = final
                async for text in result2.stream_text():
                    if not text:
                        continue
                    if text == first or (first and text.startswith(first) and text != first):
                        # restarted with same prefix — show nudge answer only
                        final = text if text != first else first
                    elif first and not text.startswith(first):
                        final = first + "\n\n" + text
                    else:
                        final = text
                    yield final
        # Turn contract: empty → one retry → BLOCKED; NO_REPLY → silent strip.
        retried_empty = False
        if anti_stall and classify_turn(final, len(called)) == "EMPTY":
            metrics.EMPTY_TURNS.inc()
            sink("empty", "retry", 0)
            retried_empty = True
            final = ""
            async with agent.run_stream(
                empty_retry_prompt(prompt), message_history=message_history,
                usage_limits=limits, model_settings=settings,
            ) as result3:
                async for text in result3.stream_text():
                    final = text
                    yield final
            if classify_turn(final, len(called)) != "EMPTY":
                try:
                    metrics.EMPTY_RETRIES.labels("recovered").inc()
                except Exception:
                    pass
        visible, kind = _apply_empty_and_silent(
            final, len(called), retried_empty=retried_empty,
        )
        if kind == "EMPTY":
            # anti_stall off, or empty never retried — still never leave silent void
            visible, kind = BLOCKED_EMPTY_MSG, "BLOCKED"
            _record_terminal("BLOCKED")
        final = visible
        if kind == "NO_REPLY":
            sink("silent", NO_REPLY_TOKEN, 0)
            yield ""  # clear any partial NO_REPLY that streamed into the bubble
        elif kind == "BLOCKED":
            sink("blocked", "empty_turn", 0)
            yield final
        elif kind == "TOOL_ONLY" and not final:
            yield ""  # tools ran; strip a lone NO_REPLY body if streamed
        metrics.AGENT_TURNS.labels("ok" if kind != "BLOCKED" else "blocked").inc()
        # F1: orchestrator memory policy (cold candidates only)
        try:
            from . import memory_policy as mp
            mp.after_turn(
                user_message=prompt if isinstance(prompt, str) else str(prompt),
                assistant_text=final or "",
                tools_called=called,
                conversation_id=conversation_id,
            )
        except Exception:
            pass
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        yield _budget_summary(turn_budget, called)
    except UnexpectedModelBehavior as e:
        metrics.AGENT_TURNS.labels("blocked").inc()
        sink("blocked", "repeated_tool", 0)
        yield _tool_retry_summary(e, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


def _finish_turn_text(
    output: str | None,
    called: list[str],
    *,
    prompt: str,
    agent: Agent,
    message_history,
    limits,
    settings,
    sink,
    anti_stall: bool,
    sync: bool = True,
) -> str:
    """Shared post-pass for run/run_async: announce nudge, empty retry, NO_REPLY strip."""
    if anti_stall and _looks_unfinished(output or "", len(called)):
        metrics.ANNOUNCE_NUDGES.inc()
        sink("nudge", "announce-without-acting", 0)
        if sync:
            result = agent.run_sync(
                _nudge_prompt(prompt, output or ""),
                message_history=message_history,
                usage_limits=limits, model_settings=settings,
            )
            output = result.output
        else:
            raise RuntimeError("_finish_turn_text sync-only for announce path")

    retried_empty = False
    if anti_stall and classify_turn(output, len(called)) == "EMPTY":
        metrics.EMPTY_TURNS.inc()
        sink("empty", "retry", 0)
        retried_empty = True
        if sync:
            result = agent.run_sync(
                empty_retry_prompt(prompt),
                message_history=message_history,
                usage_limits=limits, model_settings=settings,
            )
            output = result.output
        if classify_turn(output, len(called)) != "EMPTY":
            try:
                metrics.EMPTY_RETRIES.labels("recovered").inc()
            except Exception:
                pass

    visible, kind = _apply_empty_and_silent(
        output, len(called), retried_empty=retried_empty,
    )
    if kind == "EMPTY":
        visible, kind = BLOCKED_EMPTY_MSG, "BLOCKED"
        _record_terminal("BLOCKED")
    if kind == "NO_REPLY":
        sink("silent", NO_REPLY_TOKEN, 0)
    elif kind == "BLOCKED":
        sink("blocked", "empty_turn", 0)
    return visible


async def _finish_turn_text_async(
    output: str | None,
    called: list[str],
    *,
    prompt: str,
    agent: Agent,
    message_history,
    limits,
    settings,
    sink,
    anti_stall: bool,
) -> str:
    if anti_stall and _looks_unfinished(output or "", len(called)):
        metrics.ANNOUNCE_NUDGES.inc()
        sink("nudge", "announce-without-acting", 0)
        result = await agent.run(
            _nudge_prompt(prompt, output or ""),
            message_history=message_history,
            usage_limits=limits, model_settings=settings,
        )
        output = result.output

    retried_empty = False
    if anti_stall and classify_turn(output, len(called)) == "EMPTY":
        metrics.EMPTY_TURNS.inc()
        sink("empty", "retry", 0)
        retried_empty = True
        result = await agent.run(
            empty_retry_prompt(prompt),
            message_history=message_history,
            usage_limits=limits, model_settings=settings,
        )
        output = result.output
        if classify_turn(output, len(called)) != "EMPTY":
            try:
                metrics.EMPTY_RETRIES.labels("recovered").inc()
            except Exception:
                pass

    visible, kind = _apply_empty_and_silent(
        output, len(called), retried_empty=retried_empty,
    )
    if kind == "EMPTY":
        visible, kind = BLOCKED_EMPTY_MSG, "BLOCKED"
        _record_terminal("BLOCKED")
    if kind == "NO_REPLY":
        sink("silent", NO_REPLY_TOKEN, 0)
    elif kind == "BLOCKED":
        sink("blocked", "empty_turn", 0)
    return visible


def run(registry: Registry, prompt: str, *, model_name: str = "local",
        base_url: str = "http://localhost:4000/v1", api_key: str = "none",
        turn_budget: int = 8,
        message_history=None, enable_thinking: bool | None = None,
        on_event=None, anti_stall: bool = True, persona: str | None = None) -> str:
    selected = _gate_tools(registry.select(prompt), model_name, message_history)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event)
    agent = Agent(
        make_model(model_name, base_url, api_key=api_key),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name, registry, persona=persona),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    try:
        result = agent.run_sync(prompt, message_history=message_history,
                                usage_limits=limits, model_settings=settings)
        output = _finish_turn_text(
            result.output, called,
            prompt=prompt, agent=agent, message_history=message_history,
            limits=limits, settings=settings, sink=sink, anti_stall=anti_stall,
            sync=True,
        )
        metrics.AGENT_TURNS.labels("ok").inc()
        return output
    except UsageLimitExceeded:                        # turn budget hit -> report progress
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        return _budget_summary(turn_budget, called)
    except UnexpectedModelBehavior as e:
        metrics.AGENT_TURNS.labels("blocked").inc()
        sink("blocked", "repeated_tool", 0)
        return _tool_retry_summary(e, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)


async def run_async(registry: Registry, prompt: str, *, model_name: str = "local",
                    base_url: str = "http://localhost:4000/v1", api_key: str = "none",
                    turn_budget: int = 12,
                    message_history=None, on_event=None,
                    enable_thinking: bool | None = None, anti_stall: bool = True,
                    persona: str | None = None) -> str:
    """Non-streaming async run — for the background task runner. Same tool selection
    + watchdog + budget as run(), returns the final text. Higher default budget since
    background jobs are expected to be multi-step."""
    selected = _gate_tools(registry.select(prompt), model_name, message_history)
    metrics.TOOLS_SELECTED.observe(len(selected))
    called: list[str] = []
    sink = _make_sink(called, on_event)
    agent = Agent(
        make_model(model_name, base_url, api_key=api_key),
        tools=[t.as_pydantic_tool() for t in selected],
        system_prompt=_system_prompt(model_name, registry, persona=persona),
        capabilities=[watchdog.make_capability(on_event=sink)],
    )
    start = time.perf_counter()
    limits = UsageLimits(request_limit=turn_budget)
    settings = _thinking_settings(enable_thinking)
    try:
        result = await agent.run(prompt, message_history=message_history,
                                 usage_limits=limits, model_settings=settings)
        output = await _finish_turn_text_async(
            result.output, called,
            prompt=prompt, agent=agent, message_history=message_history,
            limits=limits, settings=settings, sink=sink, anti_stall=anti_stall,
        )
        metrics.AGENT_TURNS.labels("ok").inc()
        return output
    except UsageLimitExceeded:
        metrics.AGENT_TURNS.labels("exhausted").inc()
        metrics.NO_PROGRESS.inc()
        sink("exhausted", None, 0)
        return _budget_summary(turn_budget, called)
    except UnexpectedModelBehavior as e:
        metrics.AGENT_TURNS.labels("blocked").inc()
        sink("blocked", "repeated_tool", 0)
        return _tool_retry_summary(e, called)
    except Exception:
        metrics.AGENT_TURNS.labels("error").inc()
        raise
    finally:
        metrics.TASK_DURATION.observe(time.perf_counter() - start)
