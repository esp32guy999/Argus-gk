"""Self-observability — Argus watches its OWN Prometheus metrics in-process and alerts.

The metrics were 'native' (the harness emits from registry/loop/watchdog) but had NO
consumer, so they were theater — emitted, scraped by nobody, failures unseen. This is
the consumer, also native + TESTED, so the cornerstone actually functions and a contract
test proves it (guarding against silent rot back into cargo cult).

Reads metric values straight from the prometheus_client default registry (no HTTP self-
scrape, no external Prometheus required), evaluates a few homelab-right rules each tick,
and calls a notifier (HA push) on breach — with a per-rule cooldown so it can't spam.
An external Prometheus can still scrape /metrics for history/dashboards; this guarantees
*something* watches even when nothing else does.
"""
from __future__ import annotations

import asyncio
import os
import time

from prometheus_client import REGISTRY


def _v(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class SelfObserver:
    def __init__(self, notifier, *, interval: float = 300.0, cooldown: float = 3600.0,
                 cc_cost_alert: float | None = None):
        self.notify = notifier                      # notifier(title, message)
        self.interval = interval                    # seconds between checks
        self.cooldown = cooldown                    # min seconds between repeats of a rule
        self.cc_cost_alert = cc_cost_alert if cc_cost_alert is not None else \
            float(os.environ.get("ARGUS_CC_COST_ALERT", "5.0"))   # $ step
        self._last: dict[str, float] = {}
        self._alerted: dict[str, float] = {}
        self._cc_cost_floor = 0.0

    def _fire(self, rule: str, title: str, msg: str, now: float) -> bool:
        """Notify unless within this rule's cooldown. Returns True iff it actually sent."""
        if now - self._alerted.get(rule, -1e9) < self.cooldown:
            return False
        self._alerted[rule] = now
        try:
            self.notify(title, msg)
        except Exception:
            pass
        return True

    def check(self, now: float | None = None) -> list[str]:
        """One evaluation pass. Returns the list of rule names that fired (for tests)."""
        now = time.monotonic() if now is None else now
        cur = {
            "err":    _v("argus_agent_turns_total", {"outcome": "error"}),
            "ok":     _v("argus_agent_turns_total", {"outcome": "ok"}),
            "exh":    _v("argus_agent_turns_total", {"outcome": "exhausted"}),
            "loops":  _v("argus_agent_loop_detected_total"),
            "noprog": _v("argus_agent_no_progress_total"),
            "cc_err": _v("argus_claude_code_turns_total", {"outcome": "error"}),
        }
        cc_cost = _v("argus_claude_code_cost_usd_total")
        fired: list[str] = []

        if not self._last:                          # first pass = baseline, no deltas
            self._last = cur
            self._cc_cost_floor = cc_cost
            return fired

        d = {k: cur[k] - self._last.get(k, cur[k]) for k in cur}
        total = d["err"] + d["ok"] + d["exh"]

        if d["err"] >= 3 and total and d["err"] / total >= 0.5:
            if self._fire("agent_errors", "Argus ⚠️",
                          f"Agent error rate high: {int(d['err'])}/{int(total)} turns failed.", now):
                fired.append("agent_errors")
        if d["loops"] > 0:
            if self._fire("loops", "Argus ⚠️", f"Watchdog caught {int(d['loops'])} agent loop(s).", now):
                fired.append("loops")
        if d["noprog"] > 0:
            if self._fire("noprog", "Argus ⚠️", f"{int(d['noprog'])} no-progress give-up(s).", now):
                fired.append("noprog")
        if d["cc_err"] >= 2:
            if self._fire("cc_errors", "Argus ⚠️", f"Claude Code: {int(d['cc_err'])} failed turn(s).", now):
                fired.append("cc_errors")
        if cc_cost >= self._cc_cost_floor + self.cc_cost_alert:
            self._cc_cost_floor = cc_cost
            if self._fire("cc_cost", "Argus 💸", f"Claude Code spend passed ${cc_cost:.2f}.", now):
                fired.append("cc_cost")

        self._last = cur
        return fired

    async def run(self) -> None:
        self.check()                                # prime baseline
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.check()
            except Exception:
                pass
