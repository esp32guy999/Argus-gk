#!/usr/bin/env python3
"""Homelab health pack — real probes → structured report (+ optional SEE task).

Produces real-world data for the Supervisory Execution Engine (SEE) and ops
watchdogs. No secrets: HTTP surface checks only (login/API challenge = up).

Usage:
  PYTHONPATH=. .venv/bin/python scripts/homelab_health.py
  PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile media
  PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --profile full --see
  PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --fail-notify
  PYTHONPATH=. .venv/bin/python scripts/homelab_health.py --json-only

Exit codes: 0 all up · 1 one or more down · 2 config/runtime error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "homelab_health.yaml"
DEFAULT_REPORT_DIR = ROOT / "docs" / "health-reports"


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        # minimal fallback: require PyYAML (project has it via requirements)
        print("homelab_health: need PyYAML (pip install pyyaml)", file=sys.stderr)
        sys.exit(2)
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _probe(url: str, *, timeout: float, connect_timeout: float,
           accept: set[int]) -> dict[str, Any]:
    t0 = time.monotonic()
    status: int | None = None
    err: str | None = None
    try:
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "argus-homelab-health/1.0",
            "Accept": "*/*",
        })
        # urllib timeout is total; connect is not separate — use overall
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        status = int(e.code)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    ms = round((time.monotonic() - t0) * 1000, 1)
    ok = status is not None and status in accept
    return {
        "url": url,
        "status": status,
        "ok": ok,
        "ms": ms,
        "error": err,
    }


def _select_ids(cfg: dict, profile: str, only: list[str] | None) -> list[str]:
    if only:
        return list(only)
    profiles = cfg.get("profiles") or {}
    if profile not in profiles:
        known = ", ".join(sorted(profiles)) or "(none)"
        raise SystemExit(f"unknown profile {profile!r}; known: {known}")
    return list(profiles[profile])


def run_probes(cfg: dict, service_ids: list[str]) -> list[dict[str, Any]]:
    services = cfg.get("services") or {}
    accept = set(int(x) for x in (cfg.get("accept_statuses") or [200, 301, 302, 401]))
    timeout = float(cfg.get("timeout_s") or 4)
    connect = float(cfg.get("connect_timeout_s") or 2)
    results: list[dict[str, Any]] = []
    for sid in service_ids:
        meta = services.get(sid)
        if not meta:
            results.append({
                "id": sid, "ok": False, "status": None, "ms": 0,
                "error": "unknown service id", "criterion": sid,
                "url": "", "host": "", "role": "",
            })
            continue
        url = str(meta["url"])
        probe = _probe(url, timeout=timeout, connect_timeout=connect, accept=accept)
        results.append({
            "id": sid,
            "host": meta.get("host", ""),
            "role": meta.get("role", ""),
            "criterion": meta.get("criterion") or f"{sid} responds HTTP",
            **probe,
        })
    return results


def build_report(results: list[dict[str, Any]], *, profile: str) -> dict[str, Any]:
    up = [r for r in results if r.get("ok")]
    down = [r for r in results if not r.get("ok")]
    return {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "summary": {
            "total": len(results),
            "up": len(up),
            "down": len(down),
            "all_ok": len(down) == 0 and len(results) > 0,
        },
        "probes": results,
        "down_ids": [r["id"] for r in down],
    }


def write_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    # also keep a rolling "latest"
    latest = path.parent / "latest.json"
    latest.write_text(json.dumps(report, indent=2) + "\n")
    return path


def ha_notify(msg: str) -> bool:
    helper = os.path.expanduser("~/.local/bin/ha-remind")
    if not os.path.isfile(helper):
        return False
    try:
        subprocess.run([helper, msg[:400]], timeout=20, check=False)
        return True
    except Exception:
        return False


def see_run(results: list[dict[str, Any]], report: dict) -> dict[str, Any]:
    """Create a SEE task, attach tool:http evidence per probe, request VERIFY."""
    # Ensure repo root on path
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from argus.see import api

    criteria = [r["criterion"] for r in results]
    goal = (
        f"Homelab health ({report['profile']}): "
        f"{report['summary']['up']}/{report['summary']['total']} services up"
    )
    task = api.create(
        goal,
        success_criteria=criteria,
        checklist=criteria,
        required_evidence=criteria,
        importance=55,
        start=True,
    )
    api.checkpoint(
        task.id, "probes_started",
        detail=f"{len(results)} HTTP probes from homelab_health",
    )

    for r in results:
        cmd = f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 4 {r['url']}"
        # Evidence must prove the criterion (status + service id + port/host in blob).
        if r.get("ok"):
            summary = (
                f"{r['id']} HTTP {r['status']} (accepted) via {r['url']} "
                f"in {r['ms']}ms — service responds"
            )
            # Checklist completion is separate from evidence in SEE v1.
            api.checkpoint(
                task.id,
                f"probe_ok:{r['id']}",
                checklist_item=r["criterion"],
                detail=summary,
            )
        else:
            summary = (
                f"{r['id']} DOWN: status={r.get('status')} error={r.get('error')} "
                f"url={r['url']}"
            )
        api.add_evidence(
            task.id,
            r["criterion"],
            summary,
            kind="http",
            payload=json.dumps(r, sort_keys=True),
            source="tool:homelab_health",
            command=cmd,
            trust=0.95,
        )

    api.checkpoint(
        task.id, "probes_complete",
        detail=f"up={report['summary']['up']}/{report['summary']['total']}",
    )
    dec = api.request_verify(task.id)
    out = {
        "task_id": task.id,
        "state": api.get(task.id).current_state if api.get(task.id) else None,
        "decision": dec.to_dict(),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Homelab HTTP health pack")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--profile", default="core",
                    help="core | media | full (from config profiles)")
    ap.add_argument("--only", nargs="+", help="probe only these service ids")
    ap.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    ap.add_argument("--no-write", action="store_true", help="do not write JSON report")
    ap.add_argument("--json-only", action="store_true", help="print JSON report only")
    ap.add_argument("--see", action="store_true",
                    help="create SEE task + evidence + request_verify")
    ap.add_argument("--fail-notify", action="store_true",
                    help="HA phone push when any probe fails")
    ap.add_argument("--always-notify", action="store_true",
                    help="HA push with full summary always")
    args = ap.parse_args(argv)

    if not args.config.is_file():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2

    cfg = _load_yaml(args.config)
    try:
        ids = _select_ids(cfg, args.profile, args.only)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2

    results = run_probes(cfg, ids)
    report = build_report(results, profile=args.profile if not args.only else "custom")

    report_path = None
    if not args.no_write:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        report_path = args.report_dir / f"health-{args.profile}-{stamp}.json"
        write_report(report, report_path)
        report["report_path"] = str(report_path)

    see_out = None
    if args.see:
        try:
            see_out = see_run(results, report)
            report["see"] = see_out
            if report_path and not args.no_write:
                write_report(report, report_path)
        except Exception as e:
            report["see_error"] = f"{type(e).__name__}: {e}"
            if not args.json_only:
                print(f"SEE integration failed: {e}", file=sys.stderr)

    # notifications
    if args.always_notify or (args.fail_notify and not report["summary"]["all_ok"]):
        down = report.get("down_ids") or []
        if report["summary"]["all_ok"]:
            msg = (f"Homelab health OK ({report['profile']}): "
                   f"{report['summary']['up']}/{report['summary']['total']}")
        else:
            msg = (f"Homelab DOWN ({report['profile']}): {', '.join(down)} "
                   f"({report['summary']['up']}/{report['summary']['total']} up)")
        report["notified"] = ha_notify(msg)

    if args.json_only:
        print(json.dumps(report, indent=2))
    else:
        s = report["summary"]
        print(f"homelab_health profile={report['profile']} "
              f"{s['up']}/{s['total']} up  all_ok={s['all_ok']}")
        for r in results:
            flag = "OK " if r["ok"] else "DOWN"
            st = r.get("status") if r.get("status") is not None else "—"
            print(f"  [{flag}] {r['id']:16} HTTP {st!s:>4}  {r.get('ms', 0):>7}ms  {r.get('url')}")
            if r.get("error") and not r["ok"]:
                print(f"           error: {r['error']}")
        if report_path:
            print(f"report: {report_path}")
        if see_out:
            d = see_out.get("decision") or {}
            print(f"SEE: {see_out.get('task_id')} state={see_out.get('state')} "
                  f"action={d.get('action')} code={d.get('code')}")

    return 0 if report["summary"]["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
