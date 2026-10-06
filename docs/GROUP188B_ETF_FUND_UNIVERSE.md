# Group 188b - ETFs and funds out of the feed universe and the AngelOne sweeps

Cumulative on group 188 (Yahoo .BO misses). Item 9 of the 2026-10-06 boot-log list. Rebuild api-gateway,
market-data-service and position-stocks-service.

## What the log showed
`ALPL30IETF, BANKBETA, COMMOIETF, NV20IETF, MONQ50, LIQUIDPLUS, SILVERADD, GSEC10YEAR` (and, from item 10,
`AONESILVER, GROWWMETAL, HDFCLIQUID, LIQUIDSBI, SBILIQETF`) were in the universe. NSE lists ETFs and funds as plain EQ
series, so AngelOne's scrip master contains them. The api-gateway universe builder already dropped `*ETF` / `*BEES`
names (so ALPL30IETF, COMMOIETF, NV20IETF, SBILIQETF never came from there); the rest got through, and the two
AngelOne consumers of the whole scrip master (the movers sweep and position-stocks' WebSocket subscription) had no
filter at all.

## Change
- New `market-data-service/instrument_filter.py` (identical copy in `position-stocks-service/feed/`):
  `is_etf_or_fund(symbol)` = the old suffix patterns plus `LIQUID`, `GSEC`, `SILVERADD`/`GOLDCASE`-style names and a
  short explicit list of ETF names with no ETF-looking word (BANKBETA, MONQ50, AONESILVER, GROWWMETAL, and the ones
  real-trade-service already treats as ETFs). `ETF_FUND_EXTRA_SYMBOLS=A,B` adds names, `ETF_FUND_FILTER=0` turns it off.
- market-data-service: `_clean_feed_universe` (the symbols the Yahoo/AngelOne feeds follow) and `/angelone/movers`'
  token list skip them, which also shortens the sweep's `quote(batch)` calls.
- position-stocks-service: the WebSocket subscription skips their tokens (one INFO line with the count).
- api-gateway: `_ETF_INDEX_FUND_SYMBOL_RE` has the same patterns and names, so the universe builder, the news symbol
  extraction and the stale-cache re-filter all agree.

## Limits (read this)
This is a NAME test: there is no instrument-type field in the scrip master. Anything it does not recognise still gets
through; add it to `ETF_FUND_EXTRA_SYMBOLS` (market-data / position-stocks) - the gateway needs the name added to its
regex. The tests keep real companies with close names (SILVERTUC, GOLDIAM, KRISHNADEF, ...) unfiltered. A scalp
position already open in an ETF from before this change would not get WebSocket ticks any more.

## Tests
`market-data-service/tests/test_group188_etf_fund_filter.py`, `position-stocks-service/tests/test_group188_etf_fund_filter.py`,
`api-gateway/tests/test_group188_etf_fund_universe.py` (same symbol table in each). Full suites in the sandbox:
market-data-service 899 passed; position-stocks-service 2647 passed, 1 failed (`test_dhan_client::TestGetSdkClient::
test_valid_creds_returns_client`, fails identically on the uploaded zip); api-gateway 8286 passed, 6 failed (all
`tests/test_searched_list_selfclean.py`, order-dependent: they pass alone, and fail identically on the uploaded zip).
