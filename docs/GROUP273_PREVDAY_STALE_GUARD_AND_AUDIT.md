# Group 273 - audit of groups 268-272: stale previous-day guard, env docs, shadow-line parity

A second look at the whole opening-gate thread. What was checked and what was changed.

## Fixed
1. **Stale previous-day candle (real gap).** market-data can serve a stale last-good `/history` result, and `prev_day` took the
   last candle before today as "the previous day" whatever its date, so a candle from days ago could pass or fail the gate.
   `parse_candles(..., max_age_days)` now treats a last candle older than `OPENING_GATE_PREVDAY_MAX_AGE_DAYS` (default 6 calendar
   days, enough for a long weekend plus a holiday) as no data: it is not cached and the gate holds the entry back (fail closed),
   with an INFO line `prev_day: last candle .. is N days old`. Both services.
2. **Env vars undocumented.** `.env.example` now lists every `OPENING_GATE_*` variable (commented defaults), including the
   different shadow switch spelling per service (`1`/`0` in position-stocks-service, `true`/`false` in real-trade-service).
3. **Shadow-line parity.** The real-trade shadow log line now also carries `pd_close_pos=` and `atr_pct=` when the previous-day
   data is cached, matching the scalper's row.

## Checked, no change needed
- Candle format from market-data (`"YYYY-MM-DD HH:MM"`, keys open/high/low/close) matches what `prev_day` parses, for both the
  AngelOne and the yfinance paths.
- Every automatic entry path goes through the gated code: scalper `orders/entry.py attempt_entry`, real-trade `evaluate_mode`
  (watchlist entries are queued as candidates and pass through it). The scalper's manual-pick entry stays ungated on purpose.
- A cache miss costs one cycle: real-trade prefetches every candidate at the start of the first active cycle, the scalper warms a
  symbol the first time the gate sees it, so entries are held at most a cycle or two, not until 10:00 (unless market-data is down).
- The ENTER_AT_OPEN one-off run is only a nudge; candidates it holds back stay queued and the regular cycles pick them up.

## Tests
position-stocks-service 2986 passed; real-trade-service 3850 passed, 1 skipped. New: age-limit parsing and worker tests in both
services; the earlier real-trade shadow-line test now expects the previous-day fields.
