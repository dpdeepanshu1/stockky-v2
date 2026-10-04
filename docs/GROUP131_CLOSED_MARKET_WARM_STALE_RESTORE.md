# group131 (2026-10-04) - closed-market boot warm could never skip the sweep

Cumulative on group130. Run the api-gateway suite on the VM.

## What the post-deploy boot log showed
Market closed (Sunday), build already containing groups 120+, yet api-gateway logged
`Startup: surprise/scan cache pre-warmed (first pipeline cycle will hit cached=true)`
and NOT `Startup: market closed - restored the last surprise/scan result, skipped the boot quote sweep`.
So group 120's closed-market skip did not fire and the warm still ran `scan(cached=True)`, which falls through to the full quote sweep when nothing is restored.

## Cause (found by reading the code, not visible in the log)
`surprise_scanner` saves the last result durably with TTL `SURPRISE_CACHE_MAX_AGE_SEC + 120` (about 340 s). Group 120's closed-market branch restored it with `_load_last_result_from_durable_cache`, which uses `kv_cache.get` and so ignores any row past its TTL. Overnight, or after any restart more than ~6 minutes after the last scan, the row is expired: nothing is restored, the skip never happens, and the sweep runs. My group 120 tests used a fake loader that always "restored" something, so they could not catch this.

## Change
- `api-gateway/surprise_scanner.py`: new `_load_last_result_stale_from_durable_cache()`, identical to the existing loader but reading with `kv_cache.get_stale` (accepts an expired row). Freshness is unchanged: `scan(cached=True)` still serves a result only when its age is <= `SURPRISE_CACHE_MAX_AGE_SEC` (220 s), so a stale restore is never served as fresh.
- `api-gateway/main.py::_warm_surprise_scan_cache`: in the closed/holiday branch, if the plain load restored nothing, try the stale loader before deciding to sweep. Open, pre-open and post phases are untouched and never use it. An engine without the stale loader behaves exactly as before.
- Nothing saved at all (first ever boot) still falls through to the warm sweep, as in group 120.

## Tests
- `tests/test_surprise_scanner.py::TestLoadLastResultStaleFromDurableCache` (plain read misses but stale restores; unusable payloads ignored; errors swallowed; a stale restore is older than the 220 s fresh limit). `FakeKV` gained `get_stale`/`stale`.
- `tests/test_main_ws_loops_startup.py::TestWarmSurpriseScanCache`: closed + expired result -> restored, no scan; open market never calls the stale loader.
- Sandbox has no pytest/fastapi: the real stale loader and the real warm-branch code were run standalone (restore, nothing-saved, old-engine cases pass); the test files compile, not run under pytest.

## Correction to my earlier reading of the boot logs
I said the quote burst looked gone. The pasted logs can't show that either way (both contain roughly the same number of `/quote` lines, and a stockky-hot pass alone accounts for ~104), and the missing "restored ... skipped" line says the skip did not fire. Treat the burst as unconfirmed until the next boot log after this build shows the `restored the last surprise/scan result` line with no ~1,000-call `/quote` run behind it.

## Caveat
`kv_get_stale` only helps if the expired row still exists in the durable store; I could not check whether anything purges expired `stockky_kv` rows. If the next boot still shows `pre-warmed` instead of `restored`, the row is being purged; the fix then is a longer TTL on that save (age is checked at read time anyway), which I can do.
