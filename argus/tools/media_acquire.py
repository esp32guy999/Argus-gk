"""Media acquire lane — search/grab boundary (separate from remonitor).

Supervisor decides whether acquisition is allowed; this tool only:
  - verifies entity is monitored (else refuse → remonitor first)
  - issues SeriesSearch / MoviesSearch
  - returns evidence with search_requested + post-condition

Does NOT change monitored flags — use media_remonitor_apply for that.
"""
from __future__ import annotations

from typing import Any

from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool
from . import media_remonitor as ml

_DEFAULT_MANIFEST = "config/openapi.yaml"


def has_any(manifest_path: str = _DEFAULT_MANIFEST) -> bool:
    return ml.has_any(manifest_path)


def _acquire_one(
    base: str, headers: dict, service: str, entity_id: int, *, dry_run: bool,
) -> dict[str, Any]:
    if service == "radarr":
        movie = ml._get(base, headers, "v3", f"/movie/{entity_id}")
        title = movie.get("title") or "?"
        mon = bool(movie.get("monitored"))
        has_file = bool(movie.get("hasFile"))
        before = {"monitored": mon, "has_file": has_file, "missing": 0 if has_file else 1}
        if not mon:
            raise ModelRetry(
                f"media_acquire_request: radarr movie id={entity_id} ({title}) is not "
                f"monitored — call media_remonitor_apply first, then acquire."
            )
        if has_file:
            return {
                "ok": True,
                "skipped": True,
                "service": "radarr",
                "id": entity_id,
                "title": title,
                "evidence": {
                    "entity": "radarr.movie",
                    "id": entity_id,
                    "title": title,
                    "before": before,
                    "decision": "SKIP",
                    "actions": [],
                    "after_expected": {"search_requested": False},
                    "after_actual": {"search_requested": False, "has_file": True},
                    "post_condition_ok": True,
                    "reasons": [
                        "monitored=true",
                        "has_file=true",
                        "skip: already on disk",
                    ],
                    "evidence_id": ml._new_evidence_id("media-acquire"),
                    "search_requested": False,
                    "note": "file already present — no search",
                },
            }
        actions = ["request_search"]
        if not dry_run:
            ml._post(base, headers, "v3", "/command", {
                "name": "MoviesSearch",
                "movieIds": [entity_id],
            })
        evidence = {
            "entity": "radarr.movie",
            "id": entity_id,
            "title": title,
            "year": movie.get("year"),
            "before": before,
            "decision": "ACQUIRE",
            "actions": actions,
            "after_expected": {"search_requested": True, "monitored": True},
            "after_actual": {
                "search_requested": True,
                "monitored": mon,
                "has_file": has_file,
            },
            "post_condition_ok": True,  # command accepted; file arrives async
            "reasons": [
                "monitored=true",
                "has_file=false",
                "missing=true",
                "acquisition_boundary=search",
            ],
            "evidence_id": ml._new_evidence_id("media-acquire"),
            "search_requested": True,
            "note": (
                "dry run — would MoviesSearch"
                if dry_run else
                "MoviesSearch queued; download is asynchronous"
            ),
        }
        return {
            "ok": True,
            "service": "radarr",
            "id": entity_id,
            "title": title,
            "dry_run": dry_run,
            "searching": True,
            "evidence": evidence,
        }

    # sonarr
    series = ml._get(base, headers, "v3", f"/series/{entity_id}")
    title = series.get("title") or "?"
    mon = bool(series.get("monitored"))
    missing = ml._sonarr_missing_count(series)
    has_file = missing == 0
    before = {"monitored": mon, "has_file": has_file, "missing": missing}
    if not mon:
        raise ModelRetry(
            f"media_acquire_request: sonarr series id={entity_id} ({title}) is not "
            f"monitored — call media_remonitor_apply first, then acquire."
        )
    if missing == 0:
        return {
            "ok": True,
            "skipped": True,
            "service": "sonarr",
            "id": entity_id,
            "title": title,
            "evidence": {
                "entity": "sonarr.series",
                "id": entity_id,
                "title": title,
                "before": before,
                "decision": "SKIP",
                "actions": [],
                "after_expected": {"search_requested": False},
                "after_actual": {"search_requested": False, "missing": 0},
                "post_condition_ok": True,
                "reasons": ["monitored=true", "missing=0", "skip: library complete"],
                "evidence_id": ml._new_evidence_id("media-acquire"),
                "search_requested": False,
            },
        }
    actions = ["request_search"]
    if not dry_run:
        ml._post(base, headers, "v3", "/command", {
            "name": "SeriesSearch",
            "seriesId": entity_id,
        })
    evidence = {
        "entity": "sonarr.series",
        "id": entity_id,
        "title": title,
        "before": before,
        "decision": "ACQUIRE",
        "actions": actions,
        "after_expected": {"search_requested": True, "monitored": True},
        "after_actual": {"search_requested": True, "monitored": mon, "missing": missing},
        "post_condition_ok": True,
        "reasons": [
            "monitored=true",
            f"missing_episodes={missing}",
            "acquisition_boundary=search",
        ],
        "evidence_id": ml._new_evidence_id("media-acquire"),
        "search_requested": True,
        "note": (
            "dry run — would SeriesSearch"
            if dry_run else
            "SeriesSearch queued; download is asynchronous"
        ),
    }
    return {
        "ok": True,
        "service": "sonarr",
        "id": entity_id,
        "title": title,
        "dry_run": dry_run,
        "searching": True,
        "evidence": evidence,
    }


def _make_tools(manifest_path: str) -> list[Tool]:

    def media_acquire_request(
        service: str,
        ids: str,
        dry_run: bool = False,
        allow_broad_scope: bool = False,
    ) -> dict:
        """Request *arr search for already-monitored items (acquisition boundary).

        service: sonarr | radarr (not both — one service per call).
        ids: required comma-separated library ids (supervisor-approved).
        Requires monitored=true; refuses otherwise (remonitor first).
        More than 10 ids needs allow_broad_scope=true.
        Does not change monitor flags. Downloads are asynchronous.
        """
        service = (service or "").lower().strip()
        if service not in ("sonarr", "radarr"):
            raise ModelRetry(
                "media_acquire_request: service must be 'sonarr' or 'radarr' "
                "(one service per call; not 'both')"
            )
        only = ml._parse_ids(ids)
        if not only:
            raise ModelRetry(
                "media_acquire_request: ids is required — pass supervisor-approved "
                "comma-separated library ids (never silent full-library acquire)"
            )
        if len(only) > 10 and not allow_broad_scope and not dry_run:
            raise ModelRetry(
                f"media_acquire_request: {len(only)} ids exceeds batch of 10 — "
                f"pass allow_broad_scope=true for large acquisitions, or dry_run=true"
            )

        svcs = ml._services(manifest_path)
        if service not in svcs:
            raise ModelRetry(f"media_acquire_request: {service} not in openapi.yaml")
        base, headers = svcs[service]

        results: list[dict] = []
        for eid in sorted(only):
            try:
                results.append(
                    _acquire_one(base, headers, service, eid, dry_run=bool(dry_run))
                )
            except ModelRetry as e:
                results.append({
                    "ok": False,
                    "service": service,
                    "id": eid,
                    "error": str(e),
                })

        ok_n = sum(1 for r in results if r.get("ok"))
        searching = sum(1 for r in results if r.get("searching"))
        return {
            "service": service,
            "dry_run": bool(dry_run),
            "requested": len(only),
            "ok": ok_n,
            "searching": searching,
            "results": results,
            "evidence": [r["evidence"] for r in results if r.get("evidence")],
            "note": (
                "dry run — no searches queued"
                if dry_run else
                f"acquisition: {searching} search command(s) queued"
            ),
        }

    return [
        Tool(
            name="media_acquire_request",
            description=(
                "Request Sonarr/Radarr search for supervisor-approved library ids. "
                "Acquisition boundary only — does not change monitored flags. "
                "Entity must already be monitored (media_remonitor_apply first). "
                "ids required. Batch >10 needs allow_broad_scope=true."
            ),
            tags=[
                "media", "sonarr", "radarr", "acquire", "search", "download",
                "grab", "library", "evidence",
            ],
            func=media_acquire_request,
            provider="media_acquire",
            example={"service": "radarr", "ids": "20", "dry_run": True},
        ),
    ]


def tools(manifest_path: str = _DEFAULT_MANIFEST) -> list[Tool]:
    if not has_any(manifest_path):
        return []
    return _make_tools(manifest_path)
