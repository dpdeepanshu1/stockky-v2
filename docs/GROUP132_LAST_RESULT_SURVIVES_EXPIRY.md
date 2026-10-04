# group132 (2026-10-04) - the saved surprise result was deleted by the read meant to restore it

Cumulative on group131. Run the api-gateway suite on the VM.

## What group131's own caveat was asking
Group131 said: "kv_get_stale only helps if the expired row still exists ... I could not check whether anything purges expired stockky_kv rows." Something does, and it is the loader group120 already called first.

## Cause (reproduced, not guessed)
`kv_cache._neon_fetch` (behind plain `kv_cache.get`) DELETEs a durable row as soon as it reads it and finds it expired. `main.py::_warm_surprise_scan_cache` (closed/holiday branch) called the plain loader first, so on an expired row it removed the only copy, and the group131 stale loader that ran next found nothing. The same purge happens on any plain read of an empty-memory process after the TTL (for example `scan(cached=True)` after a restart), and the save TTL was only `SURPRISE_CACHE_MAX_AGE_SEC + 120` (~340 s). So group131 could not restore anything overnight, and the sweep would still have run.

I reproduced this against the REAL `kv_cache` and a real SQL table (SQLite returning datetimes, like Oracle/Postgres): plain-then-stale ends with `_last_result = None` and the row gone; stale-first restores it and keeps the row. The group120/131 tests used fake loaders and a fake kv store, which cannot show this.

## Change
- `api-gateway/main.py::_warm_surprise_scan_cache`: closed/holiday branch now makes ONE read, the stale loader, first (it accepts expired rows and deletes nothing). An engine without it falls back to the plain loader. Open, pre-open and post phases are untouched. Nothing saved (first ever boot) still falls through to the warm sweep.
- `api-gateway/surprise_scanner.py`: the durable copy is now saved with a TTL of 7 days (`_last_result_durable_ttl_sec`; env `SURPRISE_LAST_RESULT_TTL_SEC`, blank or invalid = 7 days, never below the old `cached_max_age_sec + 120`). This keeps the row alive across a weekend/holiday and across any plain read. Freshness is unaffected: the only consumer of `_last_result` is `scan(cached=True)`, which age-checks `scan_ts` against `SURPRISE_CACHE_MAX_AGE_SEC` (220 s), so a days-old restore is never served as fresh.

## Tests (run for real this time: pytest, fastapi, sqlalchemy and httpx did install in the sandbox)
- New `tests/test_surprise_last_result_expiry_real_kv.py` (6 tests, real `kv_cache` + SQLite): plain read purges an expired row; plain-then-stale loses it (group131 order); stale-first restores and keeps it; the saved row outlives a weekend; a plain read 3 days later still restores. Mutation-checked: with the old TTL the two TTL tests fail.
- `tests/test_surprise_scanner.py`: TTL assertions updated (7 days; floor still `max_age + 120`; env parsing, 7 cases).
- `tests/test_main_ws_loops_startup.py`: closed-overnight test now expects stale-first with the plain loader never called; added "nothing saved still warms after one stale read".
- Full suites in the sandbox on this build: api-gateway, real-trade-service (2952), market-data-service (719), analysis-intelligence-service (2209) all pass.

## What to look for after deploy
The first boot after this build still has no row to restore (the old one was already purged), so it will log `pre-warmed` once and the next scan saves a 7-day row. From the NEXT closed-market restart on, expect `Startup: market closed - restored the last surprise/scan result, skipped the boot quote sweep` and no ~1,000-call `/quote` run behind it.
