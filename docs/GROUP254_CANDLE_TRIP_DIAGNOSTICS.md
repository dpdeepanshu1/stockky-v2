# Group 254 - candle 403 diagnostics (2026-10-08 pre-market log + /angelone/budget)

## What the log and the budget output showed
- Candle trip #1 hit on about the first candle call after boot (08:18 IST, market closed), trip #2 followed once the 30 s cooldown ended
  (60 s escalation), and `/angelone/budget` showed `trips: 2`, candle `effective_rps: 0.5` (the group 251 slowdown IS running),
  `throttle_events: 0`, `waiters: 0`, `last_wait_sec: 0.0`, quote family `trips: 0`.
- So our own candle limiter never delayed or shed a call, i.e. our rate was inside 1.0/s, burst 2 - and AngelOne still answered 403.
- Real candle volume is small: the roughly 100 `/history` lines are about 6 per symbol, but group 231 cuts every short daily period from ONE
  cached 1y/1d fetch and `60m`/`1mo` intervals are not mapped to AngelOne at all (they go to yfinance), so about one candle call per symbol.
  That is on the order of 10-20 candle calls a minute against AngelOne's documented 3/s, 180/min, 5000/h for getCandleData.
- The 403 body is plain text ("Access denied because of exceeding access rate"), the edge-gateway style rather than SmartAPI's JSON.

Conclusion: lowering our candle rate again (groups 227/251 did that twice) is guessing. Three explanations fit and none can be told apart
from the current log: (1) AngelOne's block lasts longer than our 30 s cooldown, so the first call after it is refused again; (2) the edge
counts all calls from this IP/account together (the quote feed poll alone is about 3 calls a second); (3) another client uses the same key or IP.

## What changed (market-data-service; diagnostics only, no limit, cooldown or routing change)
- `angelone_client.py`: `get_candles` counts each real getCandleData send right before the HTTP call; `get_quote` and `get_quotes_batch`
  count each real quote send. Skipped, shed and cooldown-blocked calls are not counted.
- `angelone_budget.py`: `note_candle_sent()`, `note_quote_sent()` (never raise); `trip()` for the CANDLE family now logs one extra WARNING:
  `candle 403 context - candle calls sent: N in the last 10s, N in the last 60s, N since start; quote calls sent: N in the last 10s, N in the
  last 60s; this 403 came X s after the first candle call sent once the previous cooldown ended (K call(s) sent since it ended)` (or
  `no earlier candle cooldown to compare with` on the first trip). Late 403s inside a running cooldown log nothing more, as before.
- `GET /angelone/budget` gains `candle_calls` {sent_total, sent_last_10s, sent_last_60s, tripped_on_first_call_after_cooldown} and
  `quote_calls` {sent_total, sent_last_10s, sent_last_60s}.

## How to read the next log
- `tripped_on_first_call_after_cooldown` rising with `1 call(s) sent since it ended` = AngelOne's block outlasts our cooldown (lengthen it).
- A candle trip with a high quote count in the last 10 s and a low candle count = an aggregate edge limit (the quote feed is the load).
- A trip with `no earlier candle cooldown` right after a restart = the block predates this container (a previous run's calls).
- Counts near 0 everywhere = something else uses the key or IP.

## Tests
`tests/test_group254_candle_trip_diagnostics.py`, 17 tests (counting windows, trip line with and without an earlier cooldown, calls since
the cooldown ended, late 403 silent, quote trip has no candle line, budget off, reset, never-raise wrappers, real `get_candles` /
`get_quotes_batch` counting, skipped calls not counted). Run in the sandbox with a stand-in runner and stubbed httpx/pyotp (no pytest
or network there); run `bash run_tests.sh` on the VM.
