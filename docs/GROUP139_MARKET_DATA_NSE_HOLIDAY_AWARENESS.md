# group139 (2026-10-04) - market-data-service knows NSE holidays (feeds idle, caches use the closed-market TTL)

Cumulative on group138. Application code changed in market-data-service only: rebuild market-data-service.

## Problem
market-data-service had no exchange-holiday awareness. On a weekday holiday - for example Fri 2026-10-02 (Gandhi Jayanti) -
- `market_hours.is_feed_window_ist()` said "open" from 09:05 to 15:35, so the AngelOne poll loop (whole universe about every 3 s)
  and the Yahoo stream stayed running all day for prices that cannot change, and counted against AngelOne's rate limit;
- `main.is_market_open()` said "open" 09:15-15:30, so `get_cache_ttl()` gave 300 s (not 6 h) and the history caches 900 s
  (not 6 h), so quotes and history were re-fetched all day.
The other four services already skip holidays (the 2026-09-14 fix); this was the fifth copy of the calendar that never got one.

## Change
- `market-data-service/market_hours.py`: new `_NSE_HOLIDAYS_2026` (the same 16 dates as the other copies), new
  `is_nse_holiday_ist(now)`; `is_feed_window_ist()` returns False on a holiday. `MARKET_HOURS_FEED_ALWAYS_ON=true` still
  overrides it.
- `market-data-service/main.py`: `is_market_open()` returns False on a holiday (lookup wrapped, so a failure there falls back
  to the old hours-only rule, never an exception).
- `scripts/check_holiday_lists_sync.py`: now checks SEVEN copies (adds `market_hours.py`); output on the repo as shipped:
  all 5 agree on 16 dates.
- A date missing from the list is treated as a normal trading day, so a stale list only means "polls as before", never a
  silenced feed. The list covers 2026; extend all seven copies each year.

## Tests
- New `tests/test_nse_holiday_awareness.py` (18): the holiday check (IST date from a UTC instant, naive = IST), feed window
  closed on a holiday and inside the slack, neighbouring days still open, always-on switch wins, unknown date = trading day,
  `main.is_market_open()` (the real function body, extracted from the source) on a holiday / normal day / after close /
  weekend / failing lookup, and the repo sync script passing for all five lists.
- `tests/test_market_hours.py`: its helper's base week (2026-09-28) contained Fri 2026-10-02, which is now a closed day, so
  the Friday test failed; the base moved to the week of 2026-10-05 (no holiday). Test-only change.
- Run here: both files under a home-made pytest stand-in (no pytest/fastapi in this sandbox): 18 + 14 pass. The full
  market-data suite was NOT run here; on the VM: `bash run_tests.sh` in market-data-service.

## After deploying
    docker compose up -d --build market-data-service
Nothing changes on a trading day. On the next weekday holiday (Tue 2026-10-20, Dussehra) the feed status (`in_market_window`, e.g. `/internal/yahoo-ws-status`)
reads false all day and `/quote` volume stays near zero.
