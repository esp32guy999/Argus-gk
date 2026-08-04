"""Media remonitor lane — monitor-state mutations only (no downloads).

Architecture (supervisor owns intelligence; tools stay narrow):

  media_health_scan  →  evidence objects  →  supervisor decision
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        media_remonitor   media_acquire    (ignore)
        (monitored=true)  (search/grab)

This module: probe → candidate evidence → remonitor mutation → post-condition.

Modes:
  unmonitored_missing  entity monitored=false AND missing files  (narrow default)
  unmonitored          any monitored=false
  missing              has missing files
  gaps                 union (BROAD — needs allow_broad_scope or explicit ids)

Acquisition (search/download) is deliberately NOT here — use media_acquire_request.
"""
from __future__ import annotations

import itertools
import time
from typing import Any

import httpx
import yaml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

_MODES = ("unmonitored_missing", "unmonitored", "missing", "gaps")
_BROAD_MODES = frozenset({"gaps", "missing", "unmonitored"})  # need allow_broad_scope
_SERVICES = ("sonarr", "radarr", "both")
_DEFAULT_MANIFEST = "config/openapi.yaml"

# Confidence thresholds (supervisor / health recommendations)
CONF_AUTO = 90       # auto remonitor
CONF_PROPOSE = 50    # proposal for supervisor
# < CONF_PROPOSE → IGNORE

_EVIDENCE_SEQ = itertools.count(1)


# ── HTTP / config ────────────────────────────────────────────────────────────

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
            f"media_lib: GET {path} -> HTTP {r.status_code}: {r.text[:200]}"
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
            f"media_lib: PUT {path} -> HTTP {r.status_code}: {r.text[:200]}"
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
            f"media_lib: POST {path} -> HTTP {r.status_code}: {r.text[:200]}"
        )
    return r.json() if r.content else {}


def _resolve_services(service: str, svcs: dict, *, tool: str = "media_remonitor") -> list[str]:
    service = (service or "both").lower().strip()
    if service not in _SERVICES:
        raise ModelRetry(
            f"{tool}: service must be one of {list(_SERVICES)}, got {service!r}"
        )
    if service == "both":
        names = [n for n in ("sonarr", "radarr") if n in svcs]
    else:
        names = [service] if service in svcs else []
    if not names:
        raise ModelRetry(f"{tool}: no matching sonarr/radarr entry in openapi.yaml")
    return names


def _validate_mode(mode: str, *, tool: str = "media_remonitor") -> str:
    mode = (mode or "unmonitored_missing").lower().strip()
    if mode not in _MODES:
        raise ModelRetry(
            f"{tool}: mode must be one of {list(_MODES)}, got {mode!r}"
        )
    return mode


def _limit_n(limit: Any, default: int = 50) -> int:
    try:
        return max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        return default


def _require_scope(
    mode: str,
    *,
    dry_run: bool,
    only_ids: set[int] | None,
    allow_broad_scope: bool,
    tool: str,
) -> None:
    """Broad modes without explicit ids need allow_broad_scope (writes only)."""
    if dry_run:
        return
    if only_ids is not None:
        return  # targeted by id = narrow
    if mode in _BROAD_MODES and not allow_broad_scope:
        raise ModelRetry(
            f"{tool}: mode={mode!r} is a broad library operation. "
            f"Pass allow_broad_scope=true to acknowledge, or pass ids=… to target "
            f"specific items, or dry_run=true to preview. "
            f"Prefer mode=unmonitored_missing for safe bulk remonitor."
        )


# ── Evidence + confidence ────────────────────────────────────────────────────

def _new_evidence_id(prefix: str = "media-remonitor") -> str:
    stamp = time.strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{next(_EVIDENCE_SEQ):05d}"


def _score_confidence(
    *,
    monitored: bool,
    missing: int,
    has_file: bool,
    year: int | None,
    status: str | None,
    season0_only: bool = False,
    complete_unmonitored: bool = False,
) -> tuple[int, list[str]]:
    """Deterministic confidence 0–100 with score breakdown in reasons."""
    score = 0
    reasons: list[str] = []

    if not monitored:
        score += 30
        reasons.append("score+30: unmonitored")
    if missing > 0:
        score += 30
        reasons.append(f"score+30: missing_items={missing}")
    # recently released / still airing
    y = int(year) if year else 0
    current = int(time.strftime("%Y"))
    st = (status or "").lower()
    if y >= current - 1 or st in ("continuing", "incinemas", "in cinemas", "announced"):
        score += 20
        reasons.append(f"score+20: recent_or_active (year={y or '?'}, status={status or '?'})")

    if season0_only:
        score -= 50
        reasons.append("score-50: season_0_only_gaps")
    if complete_unmonitored:
        score -= 30
        reasons.append("score-30: likely_intentional_removal (unmonitored + complete)")

    score = max(0, min(100, score))
    return score, reasons


def _decision_from_confidence(confidence: int) -> str:
    if confidence >= CONF_AUTO:
        return "REMONITOR"       # auto-eligible
    if confidence >= CONF_PROPOSE:
        return "PROPOSE"         # supervisor review
    return "IGNORE"


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


def _sonarr_missing_count(series: dict) -> int:
    st = series.get("statistics") or {}
    have = int(st.get("episodeFileCount") or 0)
    want = int(st.get("episodeCount") or 0)
    if want <= 0:
        return 0
    return max(0, want - have)


def build_candidate_evidence(
    *,
    service: str,
    entity: str,
    item: dict,
    missing: int,
    has_file: bool,
    extra_reasons: list[str] | None = None,
    season0_only: bool = False,
) -> dict[str, Any]:
    """Build a supervisor-facing evidence object for one library entity."""
    mon = bool(item.get("monitored"))
    year = item.get("year")
    status = item.get("status")
    title = item.get("title") or item.get("artistName") or "?"
    complete_unmon = (not mon) and has_file and missing == 0

    conf, score_reasons = _score_confidence(
        monitored=mon,
        missing=missing,
        has_file=has_file,
        year=year if isinstance(year, int) else None,
        status=status if isinstance(status, str) else None,
        season0_only=season0_only,
        complete_unmonitored=complete_unmon,
    )
    decision = _decision_from_confidence(conf)

    facts: list[str] = [
        f"monitored={str(mon).lower()}",
        f"has_file={str(has_file).lower()}",
        f"missing={missing}",
        f"aired_or_released=true",
    ]
    if service == "sonarr":
        facts.append("entity=series")
        st = item.get("statistics") or {}
        facts.append(f"episode_file_count={st.get('episodeFileCount', 0)}")
        facts.append(f"episode_count={st.get('episodeCount', 0)}")
        if season0_only:
            facts.append("season>=1=false (specials only)")
        else:
            facts.append("season>=1=likely")
    else:
        facts.append("entity=movie")

    reasons = facts + score_reasons + list(extra_reasons or [])

    actions_suggested: list[str] = []
    if not mon or missing > 0:
        if not mon:
            actions_suggested.append("set_monitored")
        if service == "sonarr" and missing > 0:
            actions_suggested.append("monitor_missing_episodes")
    # acquisition is a separate tool — only suggest, never embed as remonitor action
    if missing > 0 and mon:
        actions_suggested.append("acquire_if_approved")  # handoff hint
    elif missing > 0 and not mon:
        actions_suggested.append("acquire_after_remonitor")

    after_expected: dict[str, Any] = {"monitored": True}
    if service == "sonarr":
        after_expected["missing_episodes_monitored"] = True

    return {
        "entity": entity,
        "service": service,
        "id": int(item["id"]),
        "title": title,
        "year": year,
        "status": status,
        "before": {
            "monitored": mon,
            "has_file": has_file,
            "missing": missing,
        },
        "decision": decision,
        "confidence": conf,
        "reasons": reasons,
        "actions_suggested": actions_suggested,
        "after_expected": after_expected,
        "evidence_id": _new_evidence_id("media-remonitor"),
        # flat mirrors for older call sites / sorting
        "monitored": mon,
        "has_file": has_file,
        "missing": missing,
    }


def scan_library(
    manifest_path: str,
    *,
    service: str = "both",
    mode: str = "unmonitored_missing",
    limit: int = 50,
    ids: str = "",
    tool: str = "media_remonitor",
) -> list[dict[str, Any]]:
    """Shared scan → list of evidence objects (no mutations)."""
    mode = _validate_mode(mode, tool=tool)
    limit_n = _limit_n(limit)
    only = _parse_ids(ids)
    svcs = _services(manifest_path)
    names = _resolve_services(service, svcs, tool=tool)
    candidates: list[dict] = []
    for name in names:
        base, headers = svcs[name]
        if name == "sonarr":
            candidates.extend(_scan_sonarr(base, headers, mode, limit_n, only))
        else:
            candidates.extend(_scan_radarr(base, headers, mode, limit_n, only))
    candidates.sort(key=lambda c: (-c.get("confidence", 0), -c.get("missing", 0), (c.get("title") or "").lower()))
    return candidates[:limit_n]


def _scan_sonarr(
    base: str, headers: dict, mode: str, limit: int, only_ids: set[int] | None,
) -> list[dict]:
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
        has_file = missing == 0
        ev = build_candidate_evidence(
            service="sonarr",
            entity="sonarr.series",
            item=s,
            missing=missing,
            has_file=has_file,
        )
        st = s.get("statistics") or {}
        ev["episode_file_count"] = st.get("episodeFileCount")
        ev["episode_count"] = st.get("episodeCount")
        cands.append(ev)
    return cands[:limit]


def _scan_radarr(
    base: str, headers: dict, mode: str, limit: int, only_ids: set[int] | None,
) -> list[dict]:
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
        ev = build_candidate_evidence(
            service="radarr",
            entity="radarr.movie",
            item=m,
            missing=missing,
            has_file=has_file,
        )
        cands.append(ev)
    return cands[:limit]


# ── Mutations (monitor only) ─────────────────────────────────────────────────

def _apply_sonarr_monitor(
    base: str, headers: dict, cand: dict, *, dry_run: bool,
) -> dict:
    sid = cand["id"]
    actions: list[str] = []
    series = _get(base, headers, "v3", f"/series/{sid}")
    before = {
        "monitored": bool(series.get("monitored")),
        "has_file": cand.get("before", {}).get("has_file"),
        "missing": cand.get("before", {}).get("missing"),
    }

    if not series.get("monitored"):
        actions.append("set_monitored")
        if not dry_run:
            _put(base, headers, "v3", f"/series/{sid}", {**series, "monitored": True})

    episodes = _get(base, headers, "v3", "/episode", seriesId=sid)
    need_mon = [
        e for e in episodes
        if int(e.get("seasonNumber") or 0) > 0
        and not e.get("hasFile")
        and not e.get("monitored")
    ]
    mon_ids = [int(e["id"]) for e in need_mon]
    ep_reasons = [
        f"episode id={e['id']} S{e.get('seasonNumber')}E{e.get('episodeNumber')}: "
        f"monitored=false has_file=false season>=1=true"
        for e in need_mon[:12]
    ]
    if mon_ids:
        actions.append("monitor_missing_episodes")
        if not dry_run:
            _put(base, headers, "v3", "/episode/monitor", {
                "episodeIds": mon_ids,
                "monitored": True,
            })

    # post-condition verification
    after_actual: dict[str, Any]
    verify_ok: bool
    if dry_run:
        after_actual = {"monitored": True, "episodes_to_monitor": len(mon_ids)}
        verify_ok = True
    else:
        fresh = _get(base, headers, "v3", f"/series/{sid}")
        after_actual = {"monitored": bool(fresh.get("monitored"))}
        verify_ok = bool(fresh.get("monitored"))

    evidence = {
        "entity": "sonarr.series",
        "id": sid,
        "title": cand.get("title"),
        "before": before,
        "decision": "REMONITOR",
        "actions": actions,
        "after_expected": {"monitored": True},
        "after_actual": after_actual,
        "post_condition_ok": verify_ok,
        "confidence": cand.get("confidence"),
        "reasons": list(cand.get("reasons") or []) + ep_reasons,
        "evidence_id": cand.get("evidence_id") or _new_evidence_id(),
        "episodes_monitored": len(mon_ids),
        "search_requested": False,
        "note": (
            "monitor-only — use media_acquire_request to search/download"
        ),
    }
    return {
        "ok": verify_ok if not dry_run else True,
        "dry_run": dry_run,
        "service": "sonarr",
        "id": sid,
        "title": cand.get("title"),
        "evidence": evidence,
    }


def _apply_radarr_monitor(
    base: str, headers: dict, cand: dict, *, dry_run: bool,
) -> dict:
    mid = cand["id"]
    actions: list[str] = []
    movie = _get(base, headers, "v3", f"/movie/{mid}")
    before = {
        "monitored": bool(movie.get("monitored")),
        "has_file": bool(movie.get("hasFile")),
        "missing": 0 if movie.get("hasFile") else 1,
    }

    if not movie.get("monitored"):
        actions.append("set_monitored")
        if not dry_run:
            _put(base, headers, "v3", f"/movie/{mid}", {**movie, "monitored": True})

    if dry_run:
        after_actual = {"monitored": True}
        verify_ok = True
    else:
        fresh = _get(base, headers, "v3", f"/movie/{mid}")
        after_actual = {
            "monitored": bool(fresh.get("monitored")),
            "has_file": bool(fresh.get("hasFile")),
        }
        verify_ok = bool(fresh.get("monitored"))

    evidence = {
        "entity": "radarr.movie",
        "id": mid,
        "title": cand.get("title"),
        "year": cand.get("year"),
        "before": before,
        "decision": "REMONITOR",
        "actions": actions,
        "after_expected": {"monitored": True, "search_requested": False},
        "after_actual": after_actual,
        "post_condition_ok": verify_ok,
        "confidence": cand.get("confidence"),
        "reasons": list(cand.get("reasons") or []),
        "evidence_id": cand.get("evidence_id") or _new_evidence_id(),
        "search_requested": False,
        "note": "monitor-only — use media_acquire_request to search/download",
    }
    return {
        "ok": verify_ok if not dry_run else True,
        "dry_run": dry_run,
        "service": "radarr",
        "id": mid,
        "title": cand.get("title"),
        "evidence": evidence,
    }


# ── Tools ────────────────────────────────────────────────────────────────────

def _make_tools(manifest_path: str) -> list[Tool]:

    def media_remonitor_scan(
        service: str = "both",
        mode: str = "unmonitored_missing",
        limit: int = 50,
        ids: str = "",
    ) -> dict:
        """Scan Sonarr/Radarr; return evidence objects with confidence + reasons.

        Read-only. Does not change monitoring or start downloads.
        decision: REMONITOR (>=90) | PROPOSE (50–89) | IGNORE (<50).
        """
        candidates = scan_library(
            manifest_path, service=service, mode=mode, limit=limit, ids=ids,
        )
        buckets = {"REMONITOR": 0, "PROPOSE": 0, "IGNORE": 0}
        for c in candidates:
            buckets[c.get("decision", "IGNORE")] = buckets.get(c.get("decision", "IGNORE"), 0) + 1
        return {
            "mode": _validate_mode(mode),
            "service": service,
            "count": len(candidates),
            "by_decision": buckets,
            "candidates": candidates,
            "thresholds": {"auto": CONF_AUTO, "propose": CONF_PROPOSE},
            "note": (
                "evidence only — media_remonitor_apply for monitor state; "
                "media_acquire_request for search/download (supervisor-approved)"
            ),
        }

    def media_remonitor_apply(
        service: str = "both",
        mode: str = "unmonitored_missing",
        ids: str = "",
        dry_run: bool = False,
        limit: int = 50,
        allow_broad_scope: bool = False,
        min_confidence: int = 50,
    ) -> dict:
        """Set monitored=true only (series/movie + missing S1+ episodes).

        Does NOT search or download — use media_acquire_request after supervisor
        approval. Broad modes (gaps/missing/unmonitored) need allow_broad_scope=true
        unless ids= is set or dry_run=true. Skips candidates below min_confidence.
        """
        mode = _validate_mode(mode)
        only = _parse_ids(ids)
        _require_scope(
            mode,
            dry_run=bool(dry_run),
            only_ids=only,
            allow_broad_scope=bool(allow_broad_scope),
            tool="media_remonitor_apply",
        )
        try:
            min_c = max(0, min(100, int(min_confidence)))
        except (TypeError, ValueError):
            min_c = CONF_PROPOSE

        plan = scan_library(
            manifest_path, service=service, mode=mode, limit=limit, ids=ids,
        )
        # Filter by confidence — supervisor can lower min_confidence explicitly
        skipped = [c for c in plan if int(c.get("confidence") or 0) < min_c]
        plan = [c for c in plan if int(c.get("confidence") or 0) >= min_c]

        svcs = _services(manifest_path)
        results: list[dict] = []
        for cand in plan:
            base, headers = svcs[cand["service"]]
            try:
                if cand["service"] == "sonarr":
                    results.append(
                        _apply_sonarr_monitor(base, headers, cand, dry_run=bool(dry_run))
                    )
                else:
                    results.append(
                        _apply_radarr_monitor(base, headers, cand, dry_run=bool(dry_run))
                    )
            except ModelRetry as e:
                results.append({
                    "ok": False,
                    "service": cand["service"],
                    "id": cand["id"],
                    "title": cand.get("title"),
                    "error": str(e),
                    "evidence_id": cand.get("evidence_id"),
                })

        ok_n = sum(1 for r in results if r.get("ok"))
        evidence_out = [r.get("evidence") for r in results if r.get("evidence")]
        return {
            "mode": mode,
            "service": service,
            "dry_run": bool(dry_run),
            "allow_broad_scope": bool(allow_broad_scope),
            "min_confidence": min_c,
            "planned": len(plan),
            "skipped_low_confidence": len(skipped),
            "applied_ok": ok_n,
            "results": results,
            "evidence": evidence_out,
            "acquisition": {
                "started": False,
                "hint": "call media_acquire_request with approved ids for search/download",
            },
            "note": (
                "dry run — no monitor changes"
                if dry_run else
                f"monitor mutations only: {ok_n}/{len(plan)} ok; no searches started"
            ),
        }

    return [
        Tool(
            name="media_remonitor_scan",
            description=(
                "Scan Sonarr/Radarr for remonitor candidates. Returns evidence objects "
                "with confidence, reasons, and decision (REMONITOR|PROPOSE|IGNORE). "
                "Read-only — no monitor changes, no downloads."
            ),
            tags=[
                "media", "sonarr", "radarr", "remonitor", "monitor", "library",
                "missing", "scan", "evidence", "tv", "movie", "hygiene",
            ],
            func=media_remonitor_scan,
            provider="media_remonitor",
            example={"service": "both", "mode": "unmonitored_missing", "limit": 20},
        ),
        Tool(
            name="media_remonitor_apply",
            description=(
                "Set monitored=true on Sonarr/Radarr gaps (monitor-only). Does NOT search "
                "or download — use media_acquire_request after approval. Broad modes need "
                "allow_broad_scope=true. dry_run=true previews. Returns evidence with "
                "before/after and post_condition_ok."
            ),
            tags=[
                "media", "sonarr", "radarr", "remonitor", "monitor", "library",
                "missing", "tv", "movie", "hygiene", "fix", "evidence",
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
