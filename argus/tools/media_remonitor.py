"""Media remonitor lane — find library gaps and re-enable *arr monitoring.

The openapi *arr lane is read-only list/lookup; arr_acquire remonitors a *single*
title when re-requested. This lane is library hygiene: scan for unmonitored /
missing items across Sonarr + Radarr, then optionally remonitor and search.

Modes (scan + apply):
  unmonitored_missing  entity monitored=false AND missing files  (safe default)
  unmonitored          any monitored=false entity
  missing              has missing files (parent may already be monitored)
  gaps                 union of unmonitored_missing + missing

Creds from config/openapi.yaml (same as arr_acquire). Side-effecting apply has
no confirm gate — the operator/model calling apply IS authorization. Prefer
scan (or apply with dry_run=true) before bulk apply.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

_MODES = ("unmonitored_missing", "unmonitored", "missing", "gaps")
_SERVICES = ("sonarr", "radarr", "both")
_DEFAULT_MANIFEST = "config/openapi.yaml"


def _services(manifest_path: str) -> dict[str, tuple[str, dict]]:
    data = yaml.safe_load(open(manifest_path)) or {}
    out: dict[str, tuple[str, dict]] = {}
    for svc in data.get("services", []):
        name = svc.get("name")
        if name in ("sonarr", "radarr"):
            out[name] = (svc["base_url"].rstrip("/"), dict(svc.get("headers", {})))
    return out


def has_any(manifest_path: str = _DEFAULT_MANIFEST) -> bool:
    try:
        return bool(_services(manifest_path))
    except Exception:
        return False


def _parse_ids(ids: str | None) -> set[int] | None:
    if ids is None or str(ids).strip() == "":
        return None
    out: set[int] = set()
    for part in str(ids).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as e:
            raise ModelRetry(
                f"media_remonitor: invalid id {part!r} — pass comma-separated integers"
            ) from e
    return out or None


def _get(base: str, headers: dict, api: str, path: str, **params) -> Any:
    r = httpx.get(
        f"{base}/api/{api}{path}",
        headers=headers,
        params=params or None,
        timeout=45,
    )
    if r.status_code >= 400:
        raise ModelRetry(
            f"media_remonitor: GET {path} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    return r.json()


def _put(base: str, headers: dict, api: str, path: str, body: dict) -> Any:
    r = httpx.put(
        f"{base}/api/{api}{path}",
        headers=headers,
        json=body,
        timeout=45,
    )
    if r.status_code >= 400:
        raise ModelRetry(
            f"media_remonitor: PUT {path} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    return r.json() if r.content else {}


def _post(base: str, headers: dict, api: str, path: str, body: dict) -> Any:
    r = httpx.post(
        f"{base}/api/{api}{path}",
        headers=headers,
        json=body,
        timeout=45,
    )
    if r.status_code >= 400:
        raise ModelRetry(
            f"media_remonitor: POST {path} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    return r.json() if r.content else {}


def _sonarr_missing_count(series: dict) -> int:
    st = series.get("statistics") or {}
    # episodeCount = aired/known monitored-eligible; totalEpisodeCount includes unaired
    have = int(st.get("episodeFileCount") or 0)
    want = int(st.get("episodeCount") or 0)
    if want <= 0:
        return 0
    return max(0, want - have)


def _match_mode(mode: str, *, monitored: bool, missing: int) -> bool:
    if mode == "unmonitored":
        return not monitored
    if mode == "unmonitored_missing":
        return (not monitored) and missing > 0
    if mode == "missing":
        return missing > 0
    if mode == "gaps":
        return (not monitored) or missing > 0
    return False


def _scan_sonarr(base: str, headers: dict, mode: str, limit: int,
                 only_ids: set[int] | None) -> list[dict]:
    series = _get(base, headers, "v3", "/series")
    cands: list[dict] = []
    for s in series:
        sid = int(s["id"])
        if only_ids is not None and sid not in only_ids:
            continue
        mon = bool(s.get("monitored"))
        missing = _sonarr_missing_count(s)
        if not _match_mode(mode, monitored=mon, missing=missing):
            continue
        st = s.get("statistics") or {}
        reasons = []
        if not mon:
            reasons.append("series_unmonitored")
        if missing > 0:
            reasons.append(f"missing_episodes:{missing}")
        cands.append({
            "service": "sonarr",
            "id": sid,
            "title": s.get("title") or "?",
            "year": s.get("year"),
            "monitored": mon,
            "has_file": missing == 0,
            "missing": missing,
            "episode_file_count": st.get("episodeFileCount"),
            "episode_count": st.get("episodeCount"),
            "status": s.get("status"),
            "reasons": reasons,
        })
    cands.sort(key=lambda c: (-c["missing"], c["title"].lower()))
    return cands[:limit]


def _scan_radarr(base: str, headers: dict, mode: str, limit: int,
                 only_ids: set[int] | None) -> list[dict]:
    movies = _get(base, headers, "v3", "/movie")
    cands: list[dict] = []
    for m in movies:
        mid = int(m["id"])
        if only_ids is not None and mid not in only_ids:
            continue
        mon = bool(m.get("monitored"))
        has_file = bool(m.get("hasFile"))
        missing = 0 if has_file else 1
        if not _match_mode(mode, monitored=mon, missing=missing):
            continue
        reasons = []
        if not mon:
            reasons.append("movie_unmonitored")
        if missing:
            reasons.append("missing_file")
        cands.append({
            "service": "radarr",
            "id": mid,
            "title": m.get("title") or "?",
            "year": m.get("year"),
            "monitored": mon,
            "has_file": has_file,
            "missing": missing,
            "status": m.get("status"),
            "reasons": reasons,
        })
    cands.sort(key=lambda c: (c["has_file"], c["title"].lower()))
    return cands[:limit]


def _apply_sonarr(
    base: str, headers: dict, cand: dict, *, search: bool, dry_run: bool,
) -> dict:
    """Remonitor series + missing unmonitored episodes; optional SeriesSearch."""
    sid = cand["id"]
    actions: list[str] = []
    series = _get(base, headers, "v3", f"/series/{sid}")
    if not series.get("monitored"):
        actions.append("set_series_monitored")
        if not dry_run:
            _put(base, headers, "v3", f"/series/{sid}", {**series, "monitored": True})

    episodes = _get(base, headers, "v3", "/episode", seriesId=sid)
    # Specials (season 0) often intentionally unmonitored — only S1+
    need_mon = [
        e for e in episodes
        if int(e.get("seasonNumber") or 0) > 0
        and not e.get("hasFile")
        and not e.get("monitored")
    ]
    mon_ids = [int(e["id"]) for e in need_mon]
    if mon_ids:
        actions.append(f"monitor_episodes:{len(mon_ids)}")
        if not dry_run:
            _put(base, headers, "v3", "/episode/monitor", {
                "episodeIds": mon_ids,
                "monitored": True,
            })

    searched = False
    missing_now = sum(
        1 for e in episodes
        if int(e.get("seasonNumber") or 0) > 0 and not e.get("hasFile")
    )
    if search and missing_now > 0:
        actions.append("series_search")
        if not dry_run:
            _post(base, headers, "v3", "/command", {
                "name": "SeriesSearch",
                "seriesId": sid,
            })
            searched = True
        else:
            searched = True  # would search

    return {
        "service": "sonarr",
        "id": sid,
        "title": cand.get("title"),
        "ok": True,
        "dry_run": dry_run,
        "actions": actions,
        "episodes_monitored": len(mon_ids),
        "missing_episodes": missing_now,
        "searching": searched and search,
        "note": (
            f"{'would ' if dry_run else ''}remonitor series; "
            f"{'would monitor' if dry_run else 'monitored'} {len(mon_ids)} episode(s); "
            f"{'would search' if dry_run and search else ('search started' if searched else 'no search')}"
        ),
    }


def _apply_radarr(
    base: str, headers: dict, cand: dict, *, search: bool, dry_run: bool,
) -> dict:
    mid = cand["id"]
    actions: list[str] = []
    movie = _get(base, headers, "v3", f"/movie/{mid}")
    if not movie.get("monitored"):
        actions.append("set_movie_monitored")
        if not dry_run:
            _put(base, headers, "v3", f"/movie/{mid}", {**movie, "monitored": True})

    has_file = bool(movie.get("hasFile"))
    searched = False
    if search and not has_file:
        actions.append("movies_search")
        if not dry_run:
            _post(base, headers, "v3", "/command", {
                "name": "MoviesSearch",
                "movieIds": [mid],
            })
            searched = True
        else:
            searched = True

    return {
        "service": "radarr",
        "id": mid,
        "title": cand.get("title"),
        "year": cand.get("year"),
        "ok": True,
        "dry_run": dry_run,
        "actions": actions,
        "has_file": has_file,
        "searching": searched and search,
        "note": (
            f"{'would ' if dry_run else ''}remonitor movie; "
            f"{'would search' if dry_run and search and not has_file else ('search started' if searched else 'no search')}"
        ),
    }


def _resolve_services(service: str, svcs: dict) -> list[str]:
    service = (service or "both").lower().strip()
    if service not in _SERVICES:
        raise ModelRetry(
            f"media_remonitor: service must be one of {list(_SERVICES)}, got {service!r}"
        )
    if service == "both":
        names = [n for n in ("sonarr", "radarr") if n in svcs]
    else:
        names = [service] if service in svcs else []
    if not names:
        raise ModelRetry(
            "media_remonitor: no matching sonarr/radarr entry in openapi.yaml"
        )
    return names


def _validate_mode(mode: str) -> str:
    mode = (mode or "unmonitored_missing").lower().strip()
    if mode not in _MODES:
        raise ModelRetry(
            f"media_remonitor: mode must be one of {list(_MODES)}, got {mode!r}"
        )
    return mode


def _make_tools(manifest_path: str) -> list[Tool]:
    # Late-bind services so tests can pass a temp manifest; re-read each call
    # so key rotation without restart still works.

    def media_remonitor_scan(
        service: str = "both",
        mode: str = "unmonitored_missing",
        limit: int = 50,
        ids: str = "",
    ) -> dict:
        """Scan Sonarr/Radarr for library gaps that need remonitoring.

        mode: unmonitored_missing (default) | unmonitored | missing | gaps.
        service: sonarr | radarr | both. Optional ids = comma-separated filter.
        Returns candidates with id, title, reasons — does NOT change monitoring.
        """
        mode = _validate_mode(mode)
        try:
            limit_n = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit_n = 50
        only = _parse_ids(ids)
        svcs = _services(manifest_path)
        names = _resolve_services(service, svcs)
        candidates: list[dict] = []
        for name in names:
            base, headers = svcs[name]
            if name == "sonarr":
                candidates.extend(
                    _scan_sonarr(base, headers, mode, limit_n, only)
                )
            else:
                candidates.extend(
                    _scan_radarr(base, headers, mode, limit_n, only)
                )
        # re-cap after merge
        candidates = candidates[:limit_n]
        return {
            "mode": mode,
            "service": service,
            "count": len(candidates),
            "candidates": candidates,
            "note": (
                "scan only — call media_remonitor_apply to remonitor "
                "(use dry_run=true first for a plan)"
            ),
        }

    def media_remonitor_apply(
        service: str = "both",
        mode: str = "unmonitored_missing",
        ids: str = "",
        search: bool = True,
        dry_run: bool = False,
        limit: int = 50,
    ) -> dict:
        """Remonitor Sonarr/Radarr gaps found by the same mode filters as scan.

        Sets monitored=true on the series/movie (and missing unmonitored Sonarr
        episodes S1+). If search=true, starts SeriesSearch / MoviesSearch for
        items still missing files. Pass ids to target specific library ids;
        empty ids = all candidates up to limit. dry_run=true plans without writes.
        """
        mode = _validate_mode(mode)
        try:
            limit_n = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit_n = 50
        only = _parse_ids(ids)
        svcs = _services(manifest_path)
        names = _resolve_services(service, svcs)

        # Re-scan under the same mode so apply never invents targets
        plan: list[dict] = []
        for name in names:
            base, headers = svcs[name]
            if name == "sonarr":
                plan.extend(_scan_sonarr(base, headers, mode, limit_n, only))
            else:
                plan.extend(_scan_radarr(base, headers, mode, limit_n, only))
        plan = plan[:limit_n]

        results: list[dict] = []
        for cand in plan:
            base, headers = svcs[cand["service"]]
            try:
                if cand["service"] == "sonarr":
                    results.append(
                        _apply_sonarr(base, headers, cand, search=bool(search), dry_run=bool(dry_run))
                    )
                else:
                    results.append(
                        _apply_radarr(base, headers, cand, search=bool(search), dry_run=bool(dry_run))
                    )
            except ModelRetry as e:
                results.append({
                    "service": cand["service"],
                    "id": cand["id"],
                    "title": cand.get("title"),
                    "ok": False,
                    "error": str(e),
                })

        ok_n = sum(1 for r in results if r.get("ok"))
        searching_n = sum(1 for r in results if r.get("searching"))
        return {
            "mode": mode,
            "service": service,
            "dry_run": bool(dry_run),
            "search": bool(search),
            "planned": len(plan),
            "applied_ok": ok_n,
            "searching": searching_n,
            "results": results,
            "note": (
                "dry run — no changes written"
                if dry_run else
                f"remonitored {ok_n}/{len(plan)}; searches started for {searching_n}"
            ),
        }

    return [
        Tool(
            name="media_remonitor_scan",
            description=(
                "Scan Sonarr/Radarr library for unmonitored or missing items that need "
                "remonitoring. Modes: unmonitored_missing (default), unmonitored, missing, gaps. "
                "Read-only — does not change monitoring."
            ),
            tags=[
                "media", "sonarr", "radarr", "remonitor", "monitor", "library",
                "missing", "scan", "tv", "movie", "hygiene",
            ],
            func=media_remonitor_scan,
            provider="media_remonitor",
            example={"service": "both", "mode": "unmonitored_missing", "limit": 20},
        ),
        Tool(
            name="media_remonitor_apply",
            description=(
                "Remonitor Sonarr/Radarr library gaps (set monitored + optional search). "
                "Same modes as media_remonitor_scan. Use dry_run=true to preview. "
                "Pass ids to target specific items. Downloads are asynchronous."
            ),
            tags=[
                "media", "sonarr", "radarr", "remonitor", "monitor", "library",
                "missing", "search", "download", "tv", "movie", "hygiene", "fix",
            ],
            func=media_remonitor_apply,
            provider="media_remonitor",
            example={
                "service": "radarr",
                "mode": "unmonitored_missing",
                "dry_run": True,
                "limit": 10,
            },
        ),
    ]


def tools(manifest_path: str = _DEFAULT_MANIFEST) -> list[Tool]:
    if not has_any(manifest_path):
        return []
    return _make_tools(manifest_path)
