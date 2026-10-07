# Group 231 - held-position probe during a quote cooldown, one 1y candle call per symbol, Tier 1 guard

Items 1, 3 and 5 of the 14:57 IST log review. (A first attempt at this group was cut off before it reached the zip,
so all three parts are written here from scratch on top of the group 230 upload.)

## Item 3 - held positions keep AngelOne access during a quote cooldown (market-data-service)
Problem: a quote 403 started the 30-60 s quote-family cooldown for EVERY quote caller. The 9 held symbols, priced every
8 s, then went to the saturated Yahoo path (about 18 HTTP calls per tick, ReadTimeouts) - real-money stop/target data.
- `angelone_budget.position_probe_allowed(lane)`: during a QUOTE-family cooldown one POSITION-lane call may be sent per
  `ANGELONE_POSITION_PROBE_INTERVAL_S` (default 1, 0 = off), starting `ANGELONE_POSITION_PROBE_MIN_AGE_S` (default 2)
  after the trip. Other lanes, unclassified calls and the candle family are unchanged (still silent).
- A probe answered 403/429 does NOT start or extend any cooldown; it pauses probing for
  `ANGELONE_POSITION_PROBE_BACKOFF_S` (default 10) via `position_probe_denied()`.
- `angelone_client.get_quote` and `get_quotes_batch` honour both the 30 s `angelone_quote` rate-limiter cooldown and
  the budget cooldown with this exception. `main._angelone_rest_quote_first` no longer turns a held symbol away at its
  early cooldown check (it reaches `get_quote`, which decides).
- `GET /angelone/budget` shows `position_probes: {allowed, denied}`.
- Limit: a probe that succeeds returns a normal quote; nothing ends the cooldown early for other callers.

## Item 1 - short daily periods from ONE 1y/1d fetch (market-data-service/main.py)
Problem: a standard candidate asks /history for 5d/1d, 1mo/1d, 3mo/1d (and 6mo/1d from other callers): up to four
AngelOne getCandleData calls per symbol, each its own cache key, against the 1.5/s candle bucket that 403s at the open.
- When AngelOne is the source, a request for `5d`, `1mo`, `3mo` or `6mo` with `interval=1d` (no `days=`) is answered by
  ONE `1y/1d` AngelOne fetch, stored under the normal 1y key (cache + durable last-good), then sliced. Every later
  short period (and a longer cached one) is sliced from that key with no upstream call.
- `5d` joins the derive tables (7 calendar days, at least 3 bars, others still 5).
- Concurrent short periods for one symbol share the fetch (single-flight on `widen|symbol|interval`).
- If the 1y call fails with AngelOne available, it is NOT repeated for the short period; the old yfinance loop
  for the requested period still runs. AngelOne not configured / no token / candle cooldown: the old path, unchanged.
- During a candle cooldown or when every source fails, a short period with no last-good of its own is served from a
  slice of the 1y last-good (`stale=true`, `source=last_good`).
- Off: `HISTORY_WIDEN_DAILY=0`. Not used when `MAX_HISTORY_PERIOD` is below `1y`.
- Cost: each first fetch is a 1y series instead of a shorter one (about 250 rows, one call); the saving is the other
  1-3 calls per symbol. The yfinance fallback is unchanged (requested period only).

## Item 5 - Tier 1 watchlist guard (real-trade-service entry_engine/entry.py)
Decision taken from my suggestion in the review (you did not object): Tier 1 rows
- use a tighter drop limit vs the catalyst price: `WATCHLIST_TIER1_MAX_DROP_PCT` (default 0.015; 0 = use
  `WATCHLIST_MAX_DROP_PCT`). Tier 2/3 keep 3%.
- must not be down on the day: `WATCHLIST_TIER1_MIN_DAY_CHANGE_PCT` (default 0.0; `off` = no day check). An unknown
  day change still passes for Tier 1.
- Not changed: the upward band (DEEPA +6.9% vs a 6% band is the band doing its job), the 15% retire limit,
  `WATCHLIST_ADVERSE_GUARD=0` (turns both off). The held-back row stays active and is re-checked next cycle.

## Tests
market-data: `test_group231_position_probe.py` (16), `test_group231_history_widen.py` (11); `test_history_reuse.py`
fixture pins the per-period path (widen off) and isolates the durable last-good store (that file's
`test_failed_fetch_leaves_no_fresh_stamp` also failed on the group 230 upload for that reason).
real-trade: `test_group231_tier1_guard.py` (8); updated group155 (Tier 1 limit / day-change tests), group164 (Tier 3
backup tick now carries a previous close - failed on the group 230 upload), `test_watchlist_trigger.py` (-1% not -2%),
group191 (call-site count after group 230).
Suites run here with real pytest: market-data 1134 passed; real-trade 3556 passed, 1 skipped, 1 error (the group172
teardown error, identical on the unmodified upload).

## Still open from the review
2 (Yahoo fallback saturation: only relieved indirectly), 4 (cold start after a mid-session restart), 6 (news pillar
0 sources), 7 (your action: `HF_MODEL`), 8 (watch only). Unconfirmed in the log: exit-tick warning, "previous close"
hold, regime WAIT.
