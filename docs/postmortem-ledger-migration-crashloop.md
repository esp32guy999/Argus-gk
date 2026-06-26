# Post-Mortem: Ledger schema migration crash-loop (2026-06-26)

**Severity:** Medium — `argus-ui` crash-looped ~100× over ~6 min; self-healed.
**Duration:** ~10:43–10:49 EDT (~6 min).
**Resolved:** 10:49:56 EDT, automatically, once the migration code path loaded.

## Symptom

`argus-ui.service` restarted ~100 times in ~6 minutes, every instance dying on:

```
sqlite3.OperationalError: no such column: category
```

The service recovered on its own and has run clean since (the live `jobs` table
now has `category` + `next_check_ts`).

## Root Cause

**Schema-migration ordering against a live SQLite DB.** While building the Work
Ledger's external lane, `category` and `next_check_ts` were added to the `jobs`
table definition (`_SCHEMA`) and to queries. But `CREATE TABLE IF NOT EXISTS` is a
**no-op on a table that already exists** — `argus.db` already held a `jobs` table
created earlier *without* those columns. So code that queried `category` hit a
column that wasn't there, and the process crashed on the periodic path that runs
that query (the reconciler's due-check).

It self-healed only once `Store._migrate()` — which runs
`ALTER TABLE jobs ADD COLUMN category …` — was actually loaded on a restart. Before
that code path existed/loaded, every restart re-ran `CREATE TABLE IF NOT EXISTS`
(no-op) and crashed again.

## Contributing Factors

1. **The test never exercised the failing path.** `tests/test_ledger.py` builds a
   *fresh* temp DB, so the table was always created *with* the new columns — the
   migrate-an-existing-table path was never tested. The bug existed only against the
   pre-existing live DB. Green tests, crashing server.
2. **No restart-verify after a schema edit.** The schema was changed without doing a
   `systemctl --user restart` + health check before moving on, so the loop ran
   unattended.
3. **systemd's burst guard is blind to slow crashes.** `Restart=always`,
   `StartLimitBurst=5` / `StartLimitIntervalSec=10s`. Each crash took ~55s to
   manifest, so no 10s window ever saw >1 restart → the burst limit never tripped →
   ~100 restarts instead of giving up. A slow crash loop evades the default guard
   entirely.

## What Fixed It

- `Store._migrate()` ALTER-migrates missing `jobs` columns onto an existing table,
  called from `__init__` after `executescript(_SCHEMA)`. (Already in place; it's what
  ended the loop.)

## Hardening (this post-mortem)

- **systemd:** widen `StartLimitIntervalSec` so slow loops fail loud — a bad deploy
  should stop and shout, not loop silently. (Applied to `argus-ui.service`.)
- **known-traps.md:** added the SQLite-live-migration + slow-crash-loop entries.
- **Process:** after any schema change — restart the service and health-check before
  moving on; and when adding columns, test the migration against a table created at
  the *old* schema, not just a fresh one.

## Timeline (EDT)

- ~10:43 — first `no such column: category` crash; systemd begins restarting (~60s cycle).
- 10:43–10:48 — restart counter climbs to ~102; each instance runs ~55s then crashes.
- 10:49:56 — a restart loads the `_migrate()` path; `ALTER TABLE` applies; healthy.
- (now) — current PID clean, `jobs` has all columns, reconciler running.
