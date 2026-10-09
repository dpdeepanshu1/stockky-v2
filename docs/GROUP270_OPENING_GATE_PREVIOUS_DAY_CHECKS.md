# Group 270 - previous-day candle checks in the opening gate

Adds the two checks group 268 left out. They are live code (not shadow-only) in both services, and they also apply in
shadow mode, so an `OPENING_SHADOW:WOULD_ENTER` row / log line means every check passed.

## The checks (only between 09:15 and `OPENING_GATE_SETTLE_IST`, default 10:00)
1. **Previous day closed in the upper half of its own range**: `(close - low) / (high - low) >= OPENING_GATE_MIN_PREVDAY_CLOSE_POS`
   (default 0.5; 0 turns it off). A zero-range previous day skips only this check.
   Reject code: `OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos=0.20 < 0.50 (prev 2026-10-08 low=.. high=.. close=..)`.
2. **Stop not tighter than a share of the daily ATR**: `stop % >= OPENING_GATE_MIN_STOP_ATR_FRAC x daily ATR %`
   (default 0.3; 0 turns it off). Reject code: `OPENING_GATE:STOP_TOO_TIGHT:stop 0.50% < 0.30 x daily ATR 2.96% = 0.89%`.

Both fail CLOSED while the gate is active: no previous-day data -> `OPENING_GATE:NO_PREV_DAY_DATA`; no ATR (fewer than 15 daily
candles, or no ATR on the tick) -> `OPENING_GATE:NO_ATR_DATA`. After the settle time nothing here applies.

## No network call on the entry path
Previous-day data comes from market-data-service `GET /history/{symbol}?period=1mo&interval=1d` through a per-day in-memory
cache (`screening/prev_day.py` in position-stocks-service, `entry_engine/prev_day.py` in real-trade-service). A cache miss
starts one daemon-thread fetch and returns "no data" at once; the entry is held back for that scan/cycle and the next one
finds the cached value. At most 6 fetches run at once, a failed symbol is retried after 60 s, a candle dated today is ignored
(only completed days count), and the cache is keyed to the IST date so yesterday's value is never reused. Scalper: the gate
warms the cache for a symbol the first time it looks at it. Real-trade: every candidate symbol is prefetched at the start of
each cycle while the gate is active.

## Where the stop comes from
- position-stocks-service: `orders/entry.py` calls `opening_gate.stop_reject()` right after the adaptive levels are computed
  (before capital is reserved), releases the symbol lock and logs the skip like the other gate rejects. In shadow mode the
  levels are computed and the same check runs before the would-enter row is written.
- real-trade-service: the stop is the ATR-derived one (`_atr_stop_target_pct`, min 2%), passed into
  `opening_gate.reject_reason(..., stop_pct=)`. Because that stop is already ATR-based, the check rarely fires there; it
  catches a flat-fallback stop (no usable ATR) and the corporate-action ATRs the stop logic clamps. It is a safety net, not a
  new filter. The scalper is where it can really bite.

## Entered rows
The scalper's ENTERED candidate-log row now also carries `pd_close_pos=` and `atr_pct=` (when cached), so winners and losers
can be compared later.

## Env
- position-stocks-service: `MARKET_DATA_URL` (docker-compose now sets `http://market-data-service:8001`; the default is the
  hosted market-data URL), `OPENING_GATE_PREVDAY_TIMEOUT_S` (8), `OPENING_GATE_MIN_PREVDAY_CLOSE_POS` (0.5),
  `OPENING_GATE_MIN_STOP_ATR_FRAC` (0.3), and from group 273 `OPENING_GATE_PREVDAY_MAX_AGE_DAYS` (6).
- real-trade-service: the same `OPENING_GATE_*` names (it already uses `MARKET_DATA_URL`).

## Things to know
- The first scan after 09:15 for a symbol is usually held back (the fetch is still running); it is picked up on the next scan.
  With a slow or down market-data-service every symbol stays held back until 10:00, which is the intended fail-closed behaviour.
  Set `OPENING_GATE_MIN_PREVDAY_CLOSE_POS=0` and `OPENING_GATE_MIN_STOP_ATR_FRAC=0` to run without these checks.
- Each new symbol costs one `/history` call to market-data (AngelOne candle budget). Only symbols that reach the gate inside
  09:15-10:00 are fetched, and results are cached for the whole day.
- The thresholds (0.5 and 0.3) are judgement calls, not measured on live data. Nothing was run against a live feed or broker.

## Tests
position-stocks-service: 11 new tests in tests/test_group268_opening_gate.py (checks, fail-closed, switches, parsing, caching,
real-thread path, in-flight cap, retry back-off, URL) + 2 wiring tests in tests/test_entry.py. real-trade-service: 16 new tests
in tests/test_group268_opening_gate.py (checks, switches, integration, prev_day module). Suites: position-stocks 2980 passed
+ the known `group210` failure; real-trade 3847 passed, 1 skipped, 1 known `group172` error.
