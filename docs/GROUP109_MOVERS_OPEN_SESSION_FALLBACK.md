# group109 (2026-10-04) - item 20: empty Movers panel when yfinance returns nothing during the open session

Cumulative on group108. Rebuild: `docker compose build api-gateway && docker compose up -d`.

## Cause
The dashboard Movers panel (`/market/top-gainers`, `/top-losers`, `/most-active`) reads `_get_nifty50_data()`, which fetches ~150 symbols from yfinance. Outside the open session it already serves a last-known list (session33c). During the open session, if the yfinance fetch returned nothing (Yahoo blocked or rate limited from the VM, item 6), the function returned `[]` and the panel was empty, even though last-known data existed and AngelOne's whole-market sweep was reachable. NSE returning 403 is not involved: this panel never used NSE.

## Change (`api-gateway/main.py`)
New `_movers_fallback_when_yahoo_empty()`, used only when the open-session yfinance fetch came back empty:
1. **AngelOne** (`market-data-service /angelone/movers`, authenticated broker quotes, already used by the scan universe): rows `{symbol, price, change, change_pct, source: "angelone"}`. `change` is derived from ltp and pct. That endpoint lists only moves of 5% or more and has no volume.
2. else the **last-known list** (up to 7 days old, same as the pre-open path);
3. else `[]`. Never raises. Runs outside the fetch lock.

Fallback rows are not cached under the daily key and never overwrite last-known, so when yfinance recovers the next request fetches normally.

## Behaviour to know
- In fallback mode the panel shows only 5%+ movers, and **Most Active stays empty** (no volume in that feed).
- Last-known rows can be from an earlier day; the panel does not label them as stale.
- One test that pinned "all-failed fetch returns []" now expects the last-known list; that is the intended change.
- Not changed: the yfinance fetch itself (item 6 is still an environment question), the pre-open/closed path, scan-universe movers.

## Tests (`tests/test_main_scan_runner.py`, 5 new; the fixture now stubs `httpx.get`)
AngelOne rows with derived change and no caching; malformed rows skipped; error/empty falls to last-known then []; gainers/losers rank AngelOne rows and most-active is empty; fallback not used when Yahoo returned data or outside the open session.

## Run here
api-gateway: 8123 passed (was 8118).
