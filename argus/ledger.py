"""The Work Ledger reconciler — the *motion* half of the ledger (state lives in
storage.py's `jobs` table). A periodic tick refreshes each non-terminal job from
its **probe** (ground truth — never the model's word) and, on a STATE transition,
pushes a message to chat. Progress updates the board silently; only state changes
notify, per the design's "every transition" rule (transition == state change, not
every percent tick). See docs/DESIGN-work-ledger.md.

Probe handlers are intentionally small and additive: one async fn per `type`,
registered in PROBES. A handler returns a partial update dict
({"progress":…, "detail":…, "state":…}); keys it omits are left unchanged, so a
handler that can't yet tell the state just reports progress and the job keeps its
current (non-terminal) state.
"""
from __future__ import annotations
import asyncio
import time
from typing import Any, Callable

import httpx

# --- service config (Lidarr creds live in the gitignored config/openapi.yaml) ----
_lidarr_cache: tuple[str, str] | None = None

def _lidarr() -> tuple[str, str] | None:
    """(base_url, api_key) for Lidarr from config/openapi.yaml, cached. None if absent."""
    global _lidarr_cache
    if _lidarr_cache is not None:
        return _lidarr_cache or None
    try:
        import yaml
        cfg = yaml.safe_load(open("config/openapi.yaml"))
    except Exception:
        _lidarr_cache = ("", "")
        return None
    def walk(d):
        if isinstance(d, dict):
            if d.get("name") == "lidarr":
                yield d
            for v in d.values():
                yield from walk(v)
        elif isinstance(d, list):
            for v in d:
                yield from walk(v)
    svc = next(walk(cfg), None)
    if not svc:
        _lidarr_cache = ("", "")
        return None
    base = svc["base_url"].rstrip("/")
    key = svc["headers"]["X-Api-Key"]
    _lidarr_cache = (base, key)
    return _lidarr_cache


def _dig(obj: Any, path: str) -> Any:
    """Dotted-path getter into nested dict/list JSON (e.g. 'a.b.0.c')."""
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


# --- probe handlers: async (probe_cfg, client) -> partial update dict -------------
_QBT_ETA_INF = 8640000   # qBittorrent's sentinel for "unknown/infinite" ETA (100 days)

async def _probe_http_json(p: dict, client: httpx.AsyncClient) -> dict:
    """GET a JSON endpoint; if it's a list, pick the item whose name contains
    match.name_contains; read progress from progress_path; mark done at done_at.
    Reads an ETA from eta_path (qBit 'eta', seconds) into eta_s for adaptive
    scheduling. Powers qBit (via the local /qbt proxy) and any list-of-items feed."""
    r = await client.get(p["url"], timeout=10)
    r.raise_for_status()
    data = r.json()
    item = data
    m = p.get("match") or {}
    if isinstance(data, list):
        needle = (m.get("name_contains") or "").lower()
        item = next((x for x in data
                     if needle in str(x.get(m.get("name_field", "name"), "")).lower()), None)
        if item is None:
            return {"detail": "no matching item yet"}
    prog = _dig(item, p.get("progress_path", "progress"))
    out: dict = {}
    if isinstance(prog, (int, float)):
        out["progress"] = float(prog)
        if prog >= p.get("done_at", 1.0):
            out["state"] = "done"
    if p.get("eta_path"):
        eta = _dig(item, p["eta_path"])
        if isinstance(eta, (int, float)) and 0 < eta < _QBT_ETA_INF:
            out["eta_s"] = float(eta)
    name = item.get(m.get("name_field", "name")) if isinstance(item, dict) else None
    if name:
        out["detail"] = f"{name} ({out.get('progress', 0) * 100:.0f}%)"
    return out


async def _probe_lidarr_artist(p: dict, client: httpx.AsyncClient) -> dict:
    """Lidarr artist completion by track-file count. done at 100%; otherwise keep
    the job's current state (the agent/queue decides active vs blocked)."""
    creds = _lidarr()
    if not creds:
        return {"detail": "lidarr not configured"}
    base, key = creds
    r = await client.get(f"{base}/api/v1/artist/{p['artist_id']}",
                         headers={"X-Api-Key": key}, timeout=15)
    r.raise_for_status()
    s = r.json().get("statistics", {}) or {}
    have, total = s.get("trackFileCount", 0), s.get("totalTrackCount", 0)
    pct = (s.get("percentOfTracks") or 0) / 100.0
    out = {"progress": pct, "detail": f"{have}/{total} tracks ({pct * 100:.0f}%)"}
    if pct >= p.get("done_at", 1.0):
        out["state"] = "done"
    return out


PROBES: dict[str, Callable] = {
    "http_json": _probe_http_json,
    "lidarr_artist": _probe_lidarr_artist,
}

# Friendly prefixes for the chat push, by kind.
_KIND_ICON = {"download": "⬇️", "import": "📥", "bakeoff": "⚖️",
              "generation": "🖼️", "deploy": "🚀", "task": "•"}


def _transition_msg(job: dict, old_state: str, new_state: str) -> str:
    icon = _KIND_ICON.get(job["kind"], "•")
    title = job["title"]
    detail = job.get("detail") or ""
    if new_state == "done":
        return f"{icon} **{title}** — ✅ done. {detail}".strip()
    if new_state == "failed":
        return f"{icon} **{title}** — ❌ failed. {detail}".strip()
    if new_state == "blocked":
        return f"{icon} **{title}** — ⏳ blocked. {detail}".strip()
    return f"{icon} **{title}** — {old_state} → {new_state}. {detail}".strip()


# ETA-adaptive scheduling knobs (the "cron loop"). We probe a download at
# ETA*FRACTION — a bit early, so we catch completion promptly — then recompute on
# the next probe (self-correcting for the wild ETA swings early in a transfer).
ETA_FRACTION = 0.8
MIN_INTERVAL = 60.0       # never re-probe a single job faster than this
DEFAULT_INTERVAL = 300.0  # used when ETA is unknown/infinite
HEARTBEAT = 20.0          # loop tick: cheap local due-check; also picks up new jobs


def _next_check(eta_s: float | None, now: float) -> float:
    """When to next probe an external job: ETA*0.8, floored at MIN_INTERVAL;
    DEFAULT_INTERVAL when ETA is unknown. This is the adaptive 'cron' time."""
    if eta_s and eta_s > 0:
        return now + max(MIN_INTERVAL, eta_s * ETA_FRACTION)
    return now + DEFAULT_INTERVAL


async def _notify_transition(store, publish, job: dict, old_state: str):
    fresh = await asyncio.to_thread(store.get_job, job["id"])
    content = _transition_msg(fresh, old_state, fresh["state"])
    cid = job.get("conversation_id") or "default"
    try:
        row = await asyncio.to_thread(store.add_message, cid, "assistant", content, None)
        publish("chat_message", {"role": "assistant", "content": content, "model": None,
                                 "conversation_id": cid, "id": row.get("id")})
    except Exception:
        pass


async def reconcile_once(store, publish, client: httpx.AsyncClient) -> int:
    """One pass of the external lane: probe only jobs that are DUE (next_check_ts
    elapsed), update the row (silent), reschedule from the fresh ETA, and on a
    STATE change push a chat_message. Network probes fire at ETA*0.8 — not every
    tick — so downloads aren't hammered. Returns # of transitions."""
    now = time.time()
    due = await asyncio.to_thread(store.due_jobs, now, category="external")
    transitions = 0
    for job in due:
        probe = job.get("probe")
        if not probe or probe.get("type") not in PROBES:
            # No probe to advance it — back off so it doesn't spin every tick.
            await asyncio.to_thread(store.update_job, job["id"],
                                    next_check_ts=now + DEFAULT_INTERVAL)
            continue
        try:
            upd = await PROBES[probe["type"]](probe, client)
        except Exception as e:
            upd = {"detail": f"probe error: {type(e).__name__}: {e}"}
        eta_s = upd.pop("eta_s", None)          # not a column — drives scheduling only
        upd["next_check_ts"] = _next_check(eta_s, now)
        old_state = job["state"]
        new_state = upd.get("state", old_state)
        await asyncio.to_thread(store.update_job, job["id"], **upd)
        if new_state != old_state:
            transitions += 1
            await _notify_transition(store, publish, job, old_state)
    return transitions


async def reconcile_loop(store, publish, heartbeat: float = HEARTBEAT):
    """Heartbeat loop for the external lane. Wakes every ~20s (cheap: a local SQL
    due-check; picks up newly-registered jobs), but only hits the network for jobs
    whose ETA-scheduled next_check_ts has elapsed. Started from the server lifespan.
    Resilient — a bad pass logs and the loop continues; it must never take the
    server down."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await reconcile_once(store, publish, client)
            except Exception as e:
                print(f"[ledger] reconcile pass failed: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(heartbeat)
