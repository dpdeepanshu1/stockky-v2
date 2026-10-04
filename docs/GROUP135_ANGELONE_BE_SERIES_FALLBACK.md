# group135 (2026-10-04) - AngelOne token lookup falls back to NSE "-BE" rows (market-data-service)

Cumulative on group134. Application code changed in market-data-service only: rebuild that service.

## Why
group133 report (B1/B2): the AngelOne feed logged `unresolved: HFCL, MTARTECH` / `HFCL, HINDCON, SHREETNB, STLTECH, WARDINMOBI`.
AngelOne's scrip master has HFCL, MTARTECH and STLTECH on NSE only as `HFCL-BE`, `MTARTECH-BE`, `STLTECH-BE`
(trade-to-trade series) plus a BSE row. The loader kept only NSE `-EQ`, so these names never got a live tick and every
`/quote`, history and waterfall lookup for them skipped AngelOne and fell through to Yahoo.

## Change (`services/market-data-service/angelone_scrip_master.py`)
- The streaming parse now builds two maps in one pass: the unchanged `-EQ` map and a `-BE` map holding NSE `-BE` rows for
  names that have NO `-EQ` row (if both exist, `-EQ` wins). BSE rows are never used.
- `get_token()` and `get_tokens_bulk()` try `-EQ` first, then the `-BE` fallback. That covers the live WS feed, /quote
  waterfall, bulk quote path and history.
- `get_all_symbols()` is deliberately still `-EQ` only: the whole-market movers sweep keeps the same universe (adding
  every trade-to-trade name would change what appears in movers).
- The disk snapshot now also stores `be_map`; an older snapshot without it still warm-starts (BE fallback fills on the
  next refresh, at most a day). `status()` shows `be_fallback_symbols`.
- position-stocks-service has its own scrip master and is NOT changed: it feeds the trading side, and BE (delivery-only)
  names must not become intraday candidates.
- `scripts/diagnose_g132_scrip.py` now defaults to all six unresolved names and prints whether a `-BE` fallback exists.

## Tests
`tests/test_angelone_scrip_master.py`: 14 passed (5 new: EQ/BE split and EQ-wins, BE-only names resolve via get_token and
get_tokens_bulk but not get_all_symbols, BE map survives a snapshot restart, old snapshot without be_map, garbage be_map).
market-data-service full suite here: 724 passed (was 719). api-gateway env/drift sweeps still pass.

## After deploying (on the VM, from ~/stockky-v2)
    docker compose up -d --build market-data-service
    docker compose logs market-data-service 2>&1 | grep -E "scrip master loaded|resolved .* requested symbols" | tail -5
Expect `scrip master loaded: ~2700 NSE-EQ symbols (+N -BE fallback)` and the feed's unresolved list to lose HFCL,
MTARTECH, STLTECH. HINDCON, SHREETNB and WARDINMOBI are not explained by this change; to see their rows run
`bash scripts/diagnose_group132.sh 2>&1 | tee g135_report.txt` (it now looks all six up) and paste it back.
