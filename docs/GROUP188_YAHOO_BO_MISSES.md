# Group 188 - stop asking Yahoo for NAME.BO when NAME.NS has nothing

Cumulative on group 187. Item 10 of the 2026-10-06 boot-log list. Rebuild market-data-service.

## What the log showed
About 15 lines like `$CHEMICAL.BO / AJOONI.BO / SBILIQETF.BO ...: possibly delisted; no price data found
(period=1mo)`, some twice. `_yahoo_tickers_for` returned `[NAME.NS, NAME.BO]` for every symbol, so each symbol Yahoo
did not know cost two rate-limited yfinance calls, and for an NSE equity the BSE twin of the same name almost never
exists.

## Change (`market-data-service/main.py`, `angelone_scrip_master.py`)
- `angelone_scrip_master.is_listed(symbol)`: True / False once the master is loaded, None before. It never loads or
  waits, so it is safe on the hot path.
- A symbol that is in the NSE scrip master (EQ or the -BE fallback) is NSE-listed: `.BO` is not tried.
  `YAHOO_SKIP_BO_FOR_NSE=0` restores the old behaviour. Before the master has loaded (first seconds after boot) both
  candidates are still returned, as before.
- For any other symbol, a `.BO` ticker that comes back with empty history is remembered for `YAHOO_BO_MISS_TTL_S`
  (default 21600 s, 0 = off); `.BO` is left out of the candidates until it expires. A BSE-only symbol that does have
  `.BO` data is found as before and never remembered. The table holds at most 3000 entries.
- Recorded in both Yahoo history paths: `_yahoo_ohlcv_quote` and `_waterfall_yahoo_history_price`. Rate-limit errors
  raise instead of returning empty history, so they are not counted as misses.

## Not changed
- Symbols Yahoo does not know at all still get one `.NS` call per attempt; the /quote negative cache (group 161) and
  the dead-symbol lists already cover repeats of those.
- A transient empty answer for a BSE-only symbol would hide its `.BO` for up to 6 h; `YAHOO_BO_MISS_TTL_S` is the
  knob if that ever shows up.

## Tests
New `tests/test_group188_yahoo_bo_misses.py` (23 cases: `is_listed`, candidate lists, miss memory incl. expiry / off /
bound, both Yahoo paths). `tests/conftest.py` clears the miss memory between tests. Run for real in the sandbox:
the new file and the full market-data-service suite pass.
