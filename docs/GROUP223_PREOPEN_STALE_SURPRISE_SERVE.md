# Group 223 - early pre-open serves the saved surprise result instead of a live sweep (api-gateway)

Cumulative on group 222. Item 1 of the 2026-10-07 post-deploy startup-log review.
Rebuild: `docker compose build api-gateway && docker compose up -d`.
Files changed: `api-gateway/surprise_scanner.py` (three helpers + one branch in `SurpriseStockEngine.scan`), `api-gateway/tests/test_surprise_scanner.py`
(env-key list, one test renamed and narrowed, +22 cases in `TestPreopenStaleServe`).

## What the log showed
Group 222 fixed the boot-hook sweep (`Startup: market not open yet - restored ... skipped the boot quote sweep` appears). The burst of
`AngelOne-first did not price X (lane budget shed this call ...)` lines still followed, because of a different caller:
real-trade-service's first `/surprise/scan?cached=true` at about 08:58 IST found a saved result **17.6 h old** (`age 63566s`). The cached path
only honours a result younger than `SURPRISE_CACHE_MAX_AGE_SEC` (220 s), and the group 138 closed-market shortcut deliberately excludes
08:30-09:15 ("that window wants live data"), so a full ~1,000-symbol live scan started. The caller waited 20 s, got
`exceeded 20s - served last computed result (age 63566s)` - the same old rows - and the scan went on in the background, calling `/quote` for every symbol and
using up the AngelOne bucket the other callers needed. (Link between the scan and the shed lines is inferred from the log order, not proven.)

## Change
In `scan(cached=True)` without `symbols`, after the age check and the group 138 check: if now is inside a trading day's 08:30-09:15 window **and more than
`SURPRISE_PREOPEN_STALE_LEAD_SEC` (default 300) before 09:15**, the saved result is returned at once with `from_cache: true`, `cache_age_sec` and
`preopen_stale_cache: true`, and no scan starts. One INFO line per 10 minutes says so. The consumer gets the same rows it got after the 20 s wait, without the wait or the sweep.
Unchanged: inside the last 5 minutes before the open, during and after the session the old rules apply; with no saved result the live scan runs as before; per-symbol requests always scan live;
any error in the check falls through to the live scan. `SURPRISE_PREOPEN_STALE_SERVE=0` or `SURPRISE_PREOPEN_STALE_LEAD_SEC=0` turns it off.

| Env (api-gateway) | Default | Meaning |
|---|---|---|
| `SURPRISE_PREOPEN_STALE_SERVE` | 1 | `0` restores the old pre-open behaviour |
| `SURPRISE_PREOPEN_STALE_LEAD_SEC` | 300 | serve stale until this many seconds before 09:15 (same default as group 222's boot lead) |

## Correction to my earlier note
I said `/angelone/movers` swept 2,584 quotes **twice** in 90 s. The log does not show that: the gateway prints its `AngelOne movers: +158 ...` line on every call, and outside market hours
market-data caches that answer for 6 h (`get_cache_ttl()`), so the second line is almost certainly a cache hit. One sweep (about 52 batch calls, background lane) is
all it costs, so I did **not** add a movers cache. The planned second half of this group is dropped.

## Not changed
- The first live scan after the open still takes ~5 min; until it finishes real-trade gets the previous session's rows, exactly as before (a result from 08:58 would also have been stale by 09:15).
- real-trade's own after-hours/ramp scan (`/quote`, `/history`, `/delivery`, `/news/analyze` per symbol, group 78) is by design and still shares the AngelOne bucket.
- The pre-open window 09:10-09:15 still scans live.

## Tests
Run `bash run_tests.sh` on the VM. No pytest/fastapi/httpx/sqlalchemy in the sandbox: under a stand-in runner the whole `test_surprise_scanner.py` gives the same 58 failures as the group 222 file
(all missing-`sqlalchemy` DB tests), the 12 `TestClosedMarketCache` cases pass (one pre-open case renamed to the last-5-minutes edge) and the 22 new cases pass.
