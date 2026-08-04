"""Media health lane — read-only library auditor.

Produces summary counts + evidence-backed recommendations for the supervisor:

  media_health_scan → evidence → REMONITOR | PROPOSE/REVIEW | IGNORE | ACQUIRE

Never mutates *arr state.
"""
from __future__ import annotations

from typing import Any

from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool
from . import media_remonitor as ml

_DEFAULT_MANIFEST = "config/openapi.yaml"


def has_any(manifest_path: str = _DEFAULT_MANIFEST) -> bool:
    return ml.has_any(manifest_path)


def _library_stats(manifest_path: str) -> dict[str, Any]:
    svcs = ml._services(manifest_path)
    out: dict[str, Any] = {}

    if "radarr" in svcs:
        base, headers = svcs["radarr"]
        movies = ml._get(base, headers, "v3", "/movie")
        mon_miss = [m for m in movies if m.get("monitored") and not m.get("hasFile")]
        unmon_miss = [m for m in movies if (not m.get("monitored")) and not m.get("hasFile")]
        unmon_have = [m for m in movies if (not m.get("monitored")) and m.get("hasFile")]
        out["movies"] = {
            "total": len(movies),
            "monitored_missing": len(mon_miss),
            "unmonitored_missing": len(unmon_miss),
            "unmonitored_complete": len(unmon_have),
            # "orphan files" would need disk walk — not available via API alone
            "orphan_files": None,
            "orphan_files_note": "not computed (no filesystem walk in this lane)",
        }

    if "sonarr" in svcs:
        base, headers = svcs["sonarr"]
        series = ml._get(base, headers, "v3", "/series")
        incomplete = []
        for s in series:
            miss = ml._sonarr_missing_count(s)
            if miss > 0:
                incomplete.append(s)
        unmon = [s for s in series if not s.get("monitored")]
        out["tv"] = {
            "total_series": len(series),
            "incomplete_series": len(incomplete),
            "unmonitored_series": len(unmon),
            "failed_imports": None,
            "failed_imports_note": "not computed (queue/history not scanned in v1)",
        }

    return out


def _make_tools(manifest_path: str) -> list[Tool]:

    def media_health_scan(
        service: str = "both",
        limit: int = 50,
    ) -> dict:
        """Read-only media library health report with scored recommendations.

        Scans Sonarr/Radarr gaps (mode=gaps), scores confidence, and buckets:
          REMONITOR (confidence>=90), REVIEW (50–89), IGNORE (<50),
          ACQUIRE (monitored + missing — handoff to media_acquire_request).
        No mutations.
        """
        try:
            limit_n = ml._limit_n(limit)
        except Exception:
            limit_n = 50

        # Full gap scan for recommendations
        evidence = ml.scan_library(
            manifest_path,
            service=service,
            mode="gaps",
            limit=limit_n,
            tool="media_health_scan",
        )

        remonitor: list[dict] = []
        review: list[dict] = []
        ignore: list[dict] = []
        acquire: list[dict] = []

        for e in evidence:
            dec = e.get("decision")
            mon = (e.get("before") or {}).get("monitored")
            missing = (e.get("before") or {}).get("missing") or e.get("missing") or 0

            # Monitored + missing → acquisition candidate (not remonitor)
            if mon and missing > 0:
                ae = dict(e)
                ae["decision"] = "ACQUIRE"
                ae["actions_suggested"] = ["request_search"]
                ae["reasons"] = list(e.get("reasons") or []) + [
                    "recommendation=ACQUIRE (already monitored, missing files)",
                ]
                acquire.append(ae)
            elif dec == "REMONITOR":
                remonitor.append(e)
            elif dec == "PROPOSE":
                re = dict(e)
                re["decision"] = "REVIEW"
                review.append(re)
            else:
                ignore.append(e)

        stats = _library_stats(manifest_path)
        return {
            "stats": stats,
            "thresholds": {
                "auto_remonitor": ml.CONF_AUTO,
                "propose": ml.CONF_PROPOSE,
            },
            "recommendations": {
                "REMONITOR": {
                    "count": len(remonitor),
                    "items": remonitor,
                    "next": "media_remonitor_apply with ids=…",
                },
                "REVIEW": {
                    "count": len(review),
                    "items": review,
                    "next": "supervisor decide; then remonitor and/or acquire",
                },
                "IGNORE": {
                    "count": len(ignore),
                    "items": ignore,
                    "next": "no action",
                },
                "ACQUIRE": {
                    "count": len(acquire),
                    "items": acquire,
                    "next": "media_acquire_request with supervisor-approved ids",
                },
            },
            "summary": {
                "remonitor": len(remonitor),
                "review": len(review),
                "ignore": len(ignore),
                "acquire": len(acquire),
            },
            "evidence_count": len(evidence),
            "note": (
                "read-only auditor — tools: media_remonitor_apply (monitor), "
                "media_acquire_request (search). Supervisor approves risky work."
            ),
        }

    return [
        Tool(
            name="media_health_scan",
            description=(
                "Read-only Sonarr/Radarr library health audit. Returns totals plus "
                "scored recommendations: REMONITOR, REVIEW, IGNORE, ACQUIRE with "
                "evidence objects (confidence + reasons). No mutations."
            ),
            tags=[
                "media", "sonarr", "radarr", "health", "audit", "library",
                "scan", "evidence", "hygiene", "tv", "movie",
            ],
            func=media_health_scan,
            provider="media_health",
            example={"service": "both", "limit": 30},
        ),
    ]


def tools(manifest_path: str = _DEFAULT_MANIFEST) -> list[Tool]:
    if not has_any(manifest_path):
        return []
    return _make_tools(manifest_path)
