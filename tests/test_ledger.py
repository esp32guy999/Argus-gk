"""Contract test for the Work Ledger (jobs table + Store methods in argus/storage.py).

Offline; uses a throwaway temp SQLite db. Validates job CRUD, the proposed→queued
approval transition, terminal-state done_ts stamping, JSON round-trip of probe/payload,
idempotent re-register (INSERT OR REPLACE keeps created_ts), and board ordering
(live work before terminal). Run: python tests/test_ledger.py
"""
from __future__ import annotations
import os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from argus.storage import Store, JOB_STATES, JOB_CATEGORIES        # noqa: E402

FAILS = []
def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond: FAILS.append(msg)
def raised(fn, *a, **k):
    try: fn(*a, **k); return False
    except Exception: return True


def main():
    db = tempfile.mktemp(suffix=".db", prefix="ledger_test_")
    try:
        s = Store(db)

        # create
        j = s.add_job("dl-test", "download", "Test DL", state="active",
                      progress=0.23, probe={"type": "http_json", "url": "x"},
                      payload={"steps": ["a", "b"]}, conversation_id="default")
        check(j["id"] == "dl-test" and j["state"] == "active", "add_job returns row")
        check(j["progress"] == 0.23, "progress stored")
        check(isinstance(j["probe"], dict) and j["probe"]["type"] == "http_json",
              "probe JSON round-trips to dict")
        check(j["payload"]["steps"] == ["a", "b"], "payload JSON round-trips")
        check(j["done_ts"] is None, "non-terminal job has no done_ts")

        # get
        check(s.get_job("dl-test")["title"] == "Test DL", "get_job by id")
        check(s.get_job("nope") is None, "get_job unknown -> None")

        # update progress + detail (non-terminal, no done_ts)
        u = s.update_job("dl-test", progress=0.5, detail="halfway")
        check(u["progress"] == 0.5 and u["detail"] == "halfway", "update patches fields")
        check(u["done_ts"] is None, "still no done_ts while active")
        check(u["updated_ts"] >= j["updated_ts"], "updated_ts advances")

        # update unknown id -> None
        check(s.update_job("ghost", progress=1.0) is None, "update unknown -> None")

        # bad state rejected
        check(raised(s.add_job, "bad", "task", "Bad", state="weird"), "bad state on add rejected")
        check(raised(lambda: s.update_job("dl-test", state="weird")), "bad state on update rejected")

        # terminal transition stamps done_ts
        d = s.update_job("dl-test", state="done", progress=1.0)
        check(d["state"] == "done" and d["done_ts"] is not None, "done stamps done_ts")

        # proposed -> queued approval flow
        p = s.add_job("plan-1", "task", "A proposed plan", state="proposed",
                      payload={"steps": ["s1", "s2"]})
        check(p["state"] == "proposed" and p["done_ts"] is None, "proposed job created")
        ap = s.update_job("plan-1", state="queued")
        check(ap["state"] == "queued", "approve flips proposed->queued")

        # idempotent re-register keeps created_ts
        created0 = s.get_job("plan-1")["created_ts"]
        time.sleep(0.01)
        re = s.add_job("plan-1", "task", "A proposed plan (re)", state="active")
        check(re["created_ts"] == created0, "re-register preserves created_ts")
        check(re["title"].endswith("(re)") and re["state"] == "active", "re-register updates fields")

        # board ordering: active/blocked/queued/proposed before terminal
        s.add_job("blk", "task", "Blocked one", state="blocked")
        ids = [r["id"] for r in s.list_jobs()]
        check(ids.index("blk") < ids.index("dl-test"), "live (blocked) ranks above terminal (done)")
        check("dl-test" in ids, "terminal job present by default")
        live_only = [r["id"] for r in s.list_jobs(include_terminal=False)]
        check("dl-test" not in live_only and "blk" in live_only, "include_terminal=False hides done")

        # every declared state is accepted by add_job
        for st in JOB_STATES:
            ok = not raised(s.add_job, f"st-{st}", "task", st, state=st)
            check(ok, f"state {st!r} accepted")

        # --- category + ETA-adaptive scheduling (the cron loop) ---------------
        check(s.add_job("ext1", "download", "Ext", category="external")["category"] == "external",
              "default/explicit external category")
        ag = s.add_job("plan-2", "task", "Agent plan", category="agent", state="proposed")
        check(ag["category"] == "agent", "agent category stored")
        check(raised(s.add_job, "bad-cat", "task", "Bad", category="nope"), "bad category rejected")

        now = time.time()
        # due: next_check_ts NULL (never scheduled) OR in the past; agent jobs excluded
        s.update_job("ext1", next_check_ts=now - 5)           # overdue
        s.add_job("ext2", "download", "Future", category="external", next_check_ts=now + 9999)
        due_ids = {j["id"] for j in s.due_jobs(now)}
        check("ext1" in due_ids, "overdue external job is due")
        check("ext2" not in due_ids, "future-scheduled job not due")
        check("plan-2" not in due_ids, "agent-category job excluded from external due set")
        s.update_job("ext2", next_check_ts=now + 100)
        # soonest scheduled is ext1 (overdue at now-5), not ext2
        check(abs(s.next_due_ts() - (now - 5)) < 1, "next_due_ts returns soonest schedule")
        # terminal external jobs drop out of both the due set and the schedule
        s.update_job("ext1", state="done")
        check("ext1" not in {j["id"] for j in s.due_jobs(time.time())}, "done job not due")
        check(abs(s.next_due_ts() - (now + 100)) < 1, "next_due_ts skips terminal jobs")

        check(set(JOB_CATEGORIES) == {"agent", "external"}, "categories are agent|external")

        # --- ledger scheduling math (the ETA-adaptive 'cron' timing) ----------
        from argus import ledger
        t0 = 1000.0
        check(ledger._next_check(None, t0) == t0 + ledger.DEFAULT_INTERVAL,
              "unknown ETA -> default interval")
        check(ledger._next_check(10, t0) == t0 + ledger.MIN_INTERVAL,
              "tiny ETA floored at MIN_INTERVAL")
        check(ledger._next_check(1000, t0) == t0 + 1000 * ledger.ETA_FRACTION,
              "ETA*0.8 when above the floor")
        # qBit '∞' eta sentinel is treated as unknown by the http_json probe parser
        item = {"name": "X", "progress": 0.5, "eta": ledger._QBT_ETA_INF}
        check(ledger._dig(item, "eta") == ledger._QBT_ETA_INF, "_dig reads nested path")
    finally:
        for ext in ("", "-wal", "-shm"):
            try: os.remove(db + ext)
            except OSError: pass

    print(f"\n{'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
