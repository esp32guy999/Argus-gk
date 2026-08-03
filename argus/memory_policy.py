"""Orchestrator-owned memory policy — what deserves the cold ledger.

Tools may still *call* flag_memory_candidate, but scoring + reject/accept lives
here so capture is consistent and testable. Post-turn ``after_turn()`` can also
flag high-value moments when the model forgot to.

Principles (expert review + specs/memory_system.md):
- Web/docs are evidence, not truth → cold candidates only, never auto-confirm.
- Score **future Shane value**, not mere durability (no Wikipedia trivia).
- Thresholds are env-tunable; defaults are conservative.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

# Minimum score (0–100) to enter the cold ledger. Below → reject (or soft-skip auto).
IMPORTANCE_MIN = int(os.environ.get("ARGUS_MEMORY_IMPORTANCE_MIN", "45"))
# Auto post-turn flags must clear a higher bar (don't spam the ledger).
AUTO_IMPORTANCE_MIN = int(os.environ.get("ARGUS_MEMORY_AUTO_MIN", "65"))
# Cap auto-flags per turn.
AUTO_MAX_PER_TURN = int(os.environ.get("ARGUS_MEMORY_AUTO_MAX", "3"))

# Expanded kinds (legacy kinds still valid). Coerced at storage boundary.
MEMORY_KINDS = (
    "dead_end", "correction", "repeat_lookup", "rule", "other",
    "config", "personal", "document", "research", "event",
)

# --- pattern libraries -------------------------------------------------------
_HOMELAB = re.compile(
    r"\b(glassgarden|anvil|nyx|sonarr|radarr|lidarr|prowlarr|navidrome|"
    r"jellyfin|qbittorrent|home\s*assistant|\bha\b|llama-swap|argus|"
    r"unraid|tailscale|p1s|bambu)\b",
    re.I,
)
_CONFIG = re.compile(
    r"\b(port\s*[:=]?\s*\d{2,5}|:\d{2,5}\b|192\.168\.\d+\.\d+|100\.\d+\.\d+\.\d+|"
    r"/mnt/user/|~/\.?[a-z]|api[_ ]?key|entity_id|docker|compose)\b",
    re.I,
)
_PERSONAL = re.compile(
    r"\b(I prefer|my\s+(vin|order|address|phone|email)|remember (this|that)|"
    r"don't forget|always use|never use|RN\d+|tesla|model\s*3)\b",
    re.I,
)
_GENERIC_WORLD = re.compile(
    r"\b(capital of|population of|who (is|was) the|weather in|stock price|"
    r"wikipedia|according to wikipedia|breaking news)\b",
    re.I,
)
_EXPLICIT_REMEMBER = re.compile(
    r"\b(remember (this|that|it)|save (this|that) (to |in )?memory|"
    r"don't forget|flag (this|that)|put that in (memory|notes))\b",
    re.I,
)
_PORT_FACT = re.compile(
    r"\b([A-Za-z][\w.-]{1,40})\s+(?:runs?|listens?|on|at|:)\s*(?:port\s*)?(\d{2,5})\b",
    re.I,
)
_IP_FACT = re.compile(
    r"\b([A-Za-z][\w.-]{1,40})\s+(?:is|at|ip)\s*((?:\d{1,3}\.){3}\d{1,3})\b",
    re.I,
)


def score_importance(
    summary: str,
    *,
    detail: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    user_text: str | None = None,
    tools_called: Iterable[str] | None = None,
) -> dict:
    """Return {importance: 0–100, kind, reasons: [str], reject: bool}.

    Pure function — no I/O. Orchestrator + tools both call this before write.
    """
    text = f"{summary or ''}\n{detail or ''}\n{user_text or ''}"
    tools = set(tools_called or [])
    score = 30  # baseline: slightly below auto threshold, above pure noise
    reasons: list[str] = []
    kind_out = kind if kind in MEMORY_KINDS else "other"

    if _EXPLICIT_REMEMBER.search(user_text or "") or _EXPLICIT_REMEMBER.search(summary or ""):
        score += 40
        reasons.append("explicit_remember")
        if kind_out == "other":
            kind_out = "personal"

    if _HOMELAB.search(text):
        score += 20
        reasons.append("homelab_entity")
        if kind_out in ("other", "research"):
            kind_out = "config"

    if _CONFIG.search(text):
        score += 25
        reasons.append("config_signal")
        if kind_out == "other":
            kind_out = "config"

    if _PERSONAL.search(text):
        score += 20
        reasons.append("personal_signal")
        if kind_out == "other":
            kind_out = "personal"

    if source and ("web_" in source or source.startswith("web") or "fetch" in source):
        score += 5
        reasons.append("from_research")
        if kind_out == "other":
            kind_out = "research"

    if "web_search" in tools or "web_fetch" in tools:
        score += 8
        reasons.append("research_tools_used")
        if kind_out == "other":
            kind_out = "research"

    if any(t in tools for t in ("save_note", "flag_memory_candidate")):
        # Model already tried to remember — slightly boost companion auto-summary
        score += 5
        reasons.append("memory_tool_used")

    if source and ("upload" in source or "document" in source or "attach" in (source or "")):
        score += 15
        reasons.append("from_document")
        if kind_out == "other":
            kind_out = "document"

    if kind in ("rule", "correction", "dead_end", "config", "personal", "document"):
        score += 15
        reasons.append(f"kind:{kind}")

    if kind == "rule":
        # Stated preferences/rules are high future-Shane value even without "remember".
        score += 15
        reasons.append("preference_rule")

    if kind == "dead_end":
        score += 5
        reasons.append("negative_memory")

    if re.search(r"\b(prefer|preference|always|never)\b", text, re.I) and not _GENERIC_WORLD.search(text):
        score += 10
        reasons.append("preference_language")

    # Penalties
    if _GENERIC_WORLD.search(text) and not _HOMELAB.search(text) and not _CONFIG.search(text):
        score -= 40
        reasons.append("generic_world_knowledge")
        if kind_out == "other":
            kind_out = "other"

    if re.search(r"\b(today'?s weather|forecast|temperature outside)\b", text, re.I):
        score -= 30
        reasons.append("ephemeral_weather")

    score = max(0, min(100, score))
    return {
        "importance": score,
        "kind": kind_out if kind_out in MEMORY_KINDS else "other",
        "reasons": reasons,
        "reject": score < IMPORTANCE_MIN,
    }


def accept_candidate(
    summary: str,
    *,
    kind: str = "other",
    detail: str | None = None,
    source: str | None = None,
    conversation_id: str | None = None,
    user_text: str | None = None,
    tools_called: Iterable[str] | None = None,
    force: bool = False,
    policy: str = "tool",
) -> dict:
    """Score + optionally write to the cold ledger. Single write entrypoint.

    Returns:
      flagged=True with id/importance/kind, or
      flagged=False, rejected=True with importance/reason.
    """
    scored = score_importance(
        summary, detail=detail, kind=kind, source=source,
        user_text=user_text, tools_called=tools_called,
    )
    if scored["reject"] and not force:
        try:
            from . import metrics
            metrics.MEMORY_CANDIDATES.labels("rejected").inc()
        except Exception:
            pass
        return {
            "flagged": False,
            "rejected": True,
            "importance": scored["importance"],
            "kind": scored["kind"],
            "reasons": scored["reasons"],
            "threshold": IMPORTANCE_MIN,
        }

    from .storage import get_store
    row = get_store().add_memory_candidate(
        scored["kind"],
        summary.strip(),
        conversation_id=conversation_id,
        detail=detail,
        source=source or policy,
        importance=scored["importance"],
        policy=policy,
    )
    try:
        from . import metrics
        metrics.MEMORY_CANDIDATES.labels("accepted").inc()
        metrics.MEMORY_IMPORTANCE.observe(scored["importance"])
    except Exception:
        pass
    return {
        "flagged": True,
        "id": row["id"],
        "kind": row["kind"],
        "importance": row.get("importance", scored["importance"]),
        "reasons": scored["reasons"],
        "policy": policy,
    }


def extract_auto_summaries(
    user_message: str,
    assistant_text: str,
    tools_called: Iterable[str] | None = None,
) -> list[dict]:
    """Heuristic extractions for post-turn auto-flag (high bar only)."""
    tools = set(tools_called or [])
    out: list[dict] = []
    blob = f"{user_message or ''}\n{assistant_text or ''}"

    if _EXPLICIT_REMEMBER.search(user_message or ""):
        # One line from assistant or user
        line = (assistant_text or user_message or "").strip().splitlines()[0][:200]
        out.append({
            "summary": line or "User asked to remember something",
            "kind": "personal",
            "detail": (assistant_text or "")[:1500],
            "source": "policy:explicit_remember",
        })

    # Config-ish port facts only if research or attach tools ran, or user asked about infra
    researchy = bool(tools & {
        "web_search", "web_fetch", "lookup_memory", "save_note", "flag_memory_candidate",
        "read_document",
    }) or bool(_HOMELAB.search(user_message or ""))

    if researchy:
        for m in _PORT_FACT.finditer(assistant_text or ""):
            host, port = m.group(1), m.group(2)
            if host.lower() in ("the", "a", "an", "on", "at", "port", "http", "https"):
                continue
            out.append({
                "summary": f"{host} port {port}",
                "kind": "config",
                "detail": m.group(0),
                "source": "policy:port_extract",
            })
        for m in _IP_FACT.finditer(assistant_text or ""):
            host, ip = m.group(1), m.group(2)
            if not ip.startswith(("192.168.", "100.", "10.")):
                continue
            out.append({
                "summary": f"{host} IP {ip}",
                "kind": "config",
                "detail": m.group(0),
                "source": "policy:ip_extract",
            })

    # Document attach markers in the prompt enrichment path
    if "Attached file" in (user_message or "") or "UNTRUSTED DOCUMENT" in (user_message or ""):
        # Prefer a short assistant claim if present
        snip = (assistant_text or "").strip().splitlines()
        snip = next((s for s in snip if len(s) > 20), "")[:200]
        if snip and not _GENERIC_WORLD.search(snip):
            out.append({
                "summary": snip,
                "kind": "document",
                "detail": "from attached document turn",
                "source": "policy:document_turn",
            })

    # Dedup by summary
    seen: set[str] = set()
    uniq = []
    for c in out:
        k = c["summary"].strip().lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq[:AUTO_MAX_PER_TURN]


def after_turn(
    *,
    user_message: str,
    assistant_text: str,
    tools_called: Iterable[str] | None = None,
    conversation_id: str | None = None,
) -> list[dict]:
    """Post-turn orchestrator pass: maybe append high-value cold candidates.

    Never promotes to facts. Never confirms. Safe to call on every chat path.
    """
    if not (user_message or assistant_text):
        return []
    tools = list(tools_called or [])
    # If the model already flagged this turn, still allow higher-value extracts
    # but skip trivial duplicates via summary match in accept.
    results = []
    for cand in extract_auto_summaries(user_message, assistant_text, tools):
        scored = score_importance(
            cand["summary"],
            detail=cand.get("detail"),
            kind=cand.get("kind"),
            source=cand.get("source"),
            user_text=user_message,
            tools_called=tools,
        )
        if scored["importance"] < AUTO_IMPORTANCE_MIN:
            try:
                from . import metrics
                metrics.MEMORY_CANDIDATES.labels("auto_skipped").inc()
            except Exception:
                pass
            continue
        # force=False still applies IMPORTANCE_MIN; auto bar is higher
        res = accept_candidate(
            cand["summary"],
            kind=scored["kind"],
            detail=cand.get("detail"),
            source=cand.get("source"),
            conversation_id=conversation_id,
            user_text=user_message,
            tools_called=tools,
            policy="orchestrator",
        )
        if res.get("flagged"):
            results.append(res)
            try:
                from . import metrics
                metrics.MEMORY_CANDIDATES.labels("auto_accepted").inc()
            except Exception:
                pass
    return results
