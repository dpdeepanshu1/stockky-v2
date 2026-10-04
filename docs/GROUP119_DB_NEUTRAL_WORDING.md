# group119 (2026-10-04) - "Neon" wording on the Oracle deployment

Cumulative on group118. Wording only; no behaviour change.

## What changed
Log lines and scan/bhavcopy status text that execute on both Neon/Postgres and Oracle no longer say "Neon":
- api-gateway `main.py` (scan status lines, Redis-fallback logs, hard-reset message, stale-universe log), `rate_limit_monitor.py`, `surprise_premarket.py` (+ market-data copy), `instant_scanner.py` reason strings;
- all six `kv_cache.py` copies ("memory + optional durable DB", "no durable DB"), identical across copies so `test_kv_cache_drift.py` still holds;
- notification-scheduler `DB keep-alive` messages, decision-prediction `decision/main.py`.

Postgres-only branches (e.g. the Neon pool-ready log, "Set DATABASE_URL ... Neon pooler URL") keep their wording. The "no URL configured" warning now also mentions the ORACLE_* env.
Existing assertions on the old strings were updated in six api-gateway test files.

## Verified
api-gateway 8155 passed, market-data 700, analysis-intelligence 2204, notification-scheduler 183. decision-prediction shows the same 2 failures (training) and 46 errors (prediction, missing `ta`) on the original upload, so they are sandbox-side.
