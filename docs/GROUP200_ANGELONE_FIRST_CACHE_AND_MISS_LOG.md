# Group 200 - AngelOne-first rows were refetched on every call; misses now say why (market-data-service)

Cumulative on group 199. Follow-up from the post-199 diagnose report (2026-10-06 08:52 UTC). Rebuild market-data-service.

## What the post-199 report shows
- Works: SGRL, KENNAMET, ROSSTECH now answer `source=angelone_rest` in 0.2-0.3 s (was 20.5 s from Yahoo).
- **Bug in group 199 (mine):** quote #2 for those three took 0.5 s, 0.7 s and 2.2 s with a NEW `fetched_at` each time, i.e. it was a fresh
  AngelOne call, not a cache hit. Cause: `_get_quote_inner` treats a cached row as soft-stale (refetch) when its remaining TTL is <= 45 s
  (`_should_soft_refresh(..., soft_window=45)`), and group 199 stored AngelOne rows with `ttl=12`, so they were refetched on every call.
  Group 199's doc said "at most one call per 12 s per symbol"; that was wrong. Each repeat `/quote` cost one `angelone_quote` token and
  could wait up to 2 s for one (the 2.2 s).
- Not explained: STEAMHOUSE still took 20.5 s from `yahoo`, so AngelOne-first returned nothing for it although its `-EQ` row resolves.
  ELEVATE came back from a `yahoo` row (not `angelone_ws` as at 08:36) and real-trade-service again logged a ReadTimeout for it
  ("per-symbol path failed for 2/11 ... ELEVATE, FINCABLES - /quotes/bulk recovered 2"). The boot log also shows
  `feed universe refresh: fetch failed, keeping existing feed: ReadTimeout`, so the live feed may not have held ELEVATE at that moment.
  The report cannot say which of these is the cause.

## Fix
- `market-data-service/main.py`: AngelOne-first rows are cached for `_AO_FIRST_FRESH_S (12) + _QUOTE_SOFT_WINDOW_S (45)` = 57 s, so they are
  served from cache for ~12 s and refreshed on the first call after that. The soft-refresh window is now the shared constant (still 45).
- New `_ao_first_miss(sym, reason)`: when AngelOne-first does not price a symbol, one INFO line per symbol per 5 min says why:
  `angelone_quote cooldown`, `no scrip-master token`, `empty answer: rate bucket busy, rate-limit cooldown or no quote for the token`,
  `answer had no positive ltp`, `called inside an event loop`, or `error <Type>` (includes the 6 s timeout). Off switch and
  not-configured stay silent. Behaviour is otherwise unchanged: every miss still falls through to the Yahoo path.

## Not changed
- Why STEAMHOUSE and ELEVATE missed: the new log line will say. Look for `AngelOne-first did not price STEAMHOUSE (...)` in market-data.
- The shared `angelone_quote` bucket (5/s, burst 8) is also used by the feed poll, `/quotes/bulk` and now every non-feed `/quote`, including
  api-gateway's per-symbol waves at boot. The 2.2 s wait for ROSSTECH shows it was nearly empty. That is item 2 and needs your decision;
  this group only stops the avoidable repeat calls.

## Tests
`tests/test_group199_quote_angelone_first.py` now has 32 (5 new): TTL outlives the soft window; with the REAL cache a repeat `/quote` is served
from cache and is refreshed once the row is ~12 s old; the miss reasons are logged once per window and named; off/not-configured are silent.
Checked that the two TTL tests fail when the TTL is put back to 12. market-data 940 passed (935 on the group 199 zip). Not live-tested.

## Check on the VM after the rebuild
Rerun `bash scripts/diagnose_never_priced.sh 2>&1 | tee never_priced_report.txt`: quote #2 for SGRL/KENNAMET/ROSSTECH should be instant with the
same `fetched_at` as #1. Then `docker compose logs --since 15m market-data-service | grep "AngelOne-first did not price"` and paste the lines.
