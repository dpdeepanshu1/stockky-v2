# group140 (2026-10-04) - NSE holiday awareness in three more places (technical, decision, position-stocks WS)

Cumulative on group139. Rebuild: `docker compose up -d --build analysis-intelligence-service decision-prediction-service position-stocks-service`
(use your compose names for those three containers).

## Problem
Group 139 fixed market-data-service. A repo-wide search for weekday-plus-hours market checks found three more that never
looked at the holiday calendar, so on a weekday holiday (Fri 2026-10-02, next Tue 2026-10-20) they behaved as if the market
were open:
- `technical/main.py` `is_market_open()` -> `get_cache_ttl()` gave 300 s instead of 6 h for technical analysis.
- `decision/main.py` `_is_market_open()` -> `_cache_ttl()` gave the open-session decision-cache TTL all day.
- `position-stocks-service/feed/ws_client.py` `_offhours_idle()` said "connect" 08:55-15:45, so the AngelOne position WebSocket
  connected and resubscribed all day for ticks that cannot arrive.
Already holiday-aware and unchanged: api-gateway (`is_nse_holiday`), real-trade-service and position-stocks `tz_utils`.

## Change
- technical and decision: new `_NSE_HOLIDAYS_2026` (same 16 dates) and a holiday check in the market-open helper.
- position ws_client: reads `tz_utils._NSE_HOLIDAYS_2026`; a holiday idles like a weekend. `POSITION_WS_OFFHOURS_IDLE=0`
  still means always connect. A failed lookup falls back to the old hours-only answer.
- `scripts/check_holiday_lists_sync.py` now checks SEVEN copies (adds technical and decision); all 7 agree on 16 dates.
- Still weekday-only and left alone on purpose: `notification-scheduler run_once.py:217` (it has its own `HOLIDAYS_2026`),
  `api-gateway/main.py:2119` and `surprise_scanner.py:1098/105` / `data_feed.py:1972` (not checked this pass; see "Not done").
- A date missing from the list counts as a trading day, so a stale list means "behaves as before".

## Tests
- New `decision/tests/test_market_open_holiday.py` (4; real function + literal extracted by ast): 4 pass here.
- `tests/test_technical_main.py` +2 (holiday closed, Dussehra closed / next day open), `tests/test_ws_offhours_idle.py` +3
  (holiday idles, off switch wins, failed lookup falls back).
- Those two files could NOT run here (no httpx/numpy/websockets/pytest in the sandbox). I ran the real function bodies
  extracted from the source with the same dates: technical `[False, 21600, False, True, 300, False]`, position
  `[True, False, False, False, True, True]` as expected. On the VM run each service's tests (`bash run_tests.sh`).
