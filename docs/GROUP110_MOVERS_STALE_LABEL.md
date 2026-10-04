# group110 (2026-10-04) - Movers panel: label last-known rows as stale

Cumulative on group109. Rebuild: `docker compose build api-gateway frontend && docker compose up -d` (frontend: your usual `npm run build` / deploy).

## Why
The group109 notes said the panel does not mark last-known rows as stale: they can be from an earlier day and looked live. The code comment on the pre-open path already said "clearly marked stale" but nothing was marked.

## Change
**api-gateway/main.py**
- New `_mark_movers_stale(rows)`: returns copies of last-known rows with `stale: true`. The stored last-known list is never changed; non-dict entries pass through.
- Used on both last-known paths in `_get_nifty50_data()`: pre-open/closed, and open session when yfinance and AngelOne both came back empty.
- New `_movers_response()` used by `/market/top-gainers`, `/top-losers`, `/most-active`: same `{data, count}`, plus `"stale": true` only when a returned row is stale.

**frontend**: `MarketResponse.stale?: boolean` (api.ts); `MarketMovers.tsx` shows "Live data unavailable - showing the last known list (may be from an earlier session)." above the rows when `stale` is true.

## Behaviour to know
- Fresh yfinance rows and AngelOne fallback rows have no `stale` key; responses for them are unchanged.
- Other callers of `_get_nifty50_data()` (the scan-universe mover seed) ignore the extra key.
- In last-known mode Most Active ranks the stored volumes (they are real, just old). Only AngelOne fallback rows have no volume.
- The label does not say how old the rows are; the last-known copy stores no timestamp. Not changed.

## Tests (`tests/test_main_scan_runner.py`)
3 new: stale marking copies and does not mutate; routes flag `stale` when last-known rows are served (including Most Active ranking); no `stale` key for fresh or AngelOne rows. 3 existing assertions updated to expect `stale: true` on last-known rows.

## Run here
No pytest/fastapi in the sandbox, so the suite was NOT run. `main.py` and the test file compile; the two new helpers were checked in isolation. The frontend was not type-checked or built (no node_modules). Run `bash run_tests.sh` in api-gateway (expect 8126 passed, was 8123) and build the frontend.
