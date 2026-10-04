# group141 (2026-10-04) - api-gateway scan-universe cache TTL treats an NSE holiday as closed

Cumulative on group140. Application code changed in api-gateway only: rebuild api-gateway.

## What I checked (the "other weekday-only checks" group 140 listed)
- `api-gateway/main.py` `_build_scan_universe` (was line ~2119): weekday + hours only -> a REAL gap. On a weekday holiday
  (Fri 2026-10-02, Tue 2026-10-20) the universe was cached for 30 min instead of 6 h, so it was rebuilt all day.
- `surprise_scanner.py:105` (`_is_trading_day`) and `:1098` (`is_market_open_ist`): already check `nse_holidays` - no change.
- `data_feed.py:1972` (`_is_nse_session_open`): already checks `nse_holidays` - no change.
- `data_feed.py:1526` / `market-data bhavcopy.py:237` (candidate bhavcopy dates): weekday-only on purpose; a holiday date just
  fails to download and the next candidate is tried (6 candidates) - no change.
- `surprise_scanner.py:363` (intraday-progress fraction for rvol): clock-only; only used while scanning, a holiday has no
  live scan to mis-score - no change.
- `indianapi_fallback.py` cache expiry: documented as safe to be wrong in the early direction - no change.
- `notification-scheduler run_once.py:217` has its own `HOLIDAYS_2026` - already aware.

## Change (`api-gateway/main.py`)
`is_weekday` becomes False on an NSE holiday (`is_nse_holiday`, already imported), so the TTL is 6 h. A failed lookup keeps the
old weekday-only answer. Nothing changes on a trading day.

## Tests (`tests/test_main_universe.py`)
- Parametrised TTL test: the old "Friday 2 Oct -> 1800" row was really a holiday, so it is now 21600; added Thu 1 Oct -> 1800
  and Tue 20 Oct (Dussehra) -> 21600.
- New: holiday lookup failure keeps the weekday-only answer (1800).
- Not run under pytest here (no pytest/fastapi in the sandbox). I ran the real TTL block from the source against the real
  `nse_holidays` module with the same dates: `[1800, 21600, 21600, 1800, 21600, 21600]`, and 1800 when the lookup raises.
  On the VM: `bash run_tests.sh` in api-gateway.

## After deploying
    docker compose up -d --build api-gateway
