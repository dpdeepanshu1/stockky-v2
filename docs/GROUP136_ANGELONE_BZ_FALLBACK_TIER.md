# group136 (2026-10-04) - AngelOne token fallback also accepts NSE "-BZ" rows (market-data-service)

Cumulative on group135. Application code changed in market-data-service only: rebuild that service.

## What the group135 report (g135_report.txt) showed
- Group 135 works: HFCL, MTARTECH, STLTECH and HINDCON each report "-BE fallback: YES", and the feed's unresolved list
  fell from 5 names to `SHREETNB, WARDINMOBI` (490/492 resolved).
- WARDINMOBI: AngelOne has `WARDINMOBI-BZ` on NSE (token 766360) plus a BSE row, no `-EQ` and no `-BE`. NSE admitted it under
  "Permitted to Trade" from 17 Aug 2026 (listed on BSE only), which is the BZ series.
- SHREETNB: no row at all in AngelOne's file (not by symbol, not by name). It is a real ticker on Yahoo (the news tests
  use SHREETNB.NS), so it is probably not yet in AngelOne's scrip master. Nothing to fix in code; the quote waterfall
  already falls through to Yahoo for it.

## Change (`angelone_scrip_master.py`)
`-BZ` rows are a last-resort tier in the same fallback map. Priority: `-EQ` > `-BE` > `-BZ`, independent of row order.
BSE rows are never used. `get_all_symbols()` (movers sweep) is still `-EQ` only. Not added: `-SM` (SME) and other series.
`scripts/diagnose_g132_scrip.py` now reports `-BE/-BZ` fallback.

## Tests
2 new in `tests/test_angelone_scrip_master.py` (priority and row-order, BZ-only name via get_token/get_tokens_bulk but not
get_all_symbols). market-data-service suite here: 726 passed.

## After deploying
    docker compose up -d --build market-data-service
    docker compose logs market-data-service 2>&1 | grep "requested symbols" | tail -2
Expect `unresolved: SHREETNB` only (or nothing, if AngelOne has added it).
