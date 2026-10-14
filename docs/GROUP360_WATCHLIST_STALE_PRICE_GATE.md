# group360 - watchlist trigger refuses a not-live price (log review item #10)

Source: the 14 Oct 10:38 IST restart log list, item 10 (stale-price gate for queued symbols). The log lines themselves are not in the
zip; the symptom, as reported in group358: GERMAN `QUEUED` with `move=0.00%`.

## What the code did
`evaluate_watchlist_entries` (real-trade-service `entry_engine/entry.py`) takes ticks with `allow_stale=True` (a last-good copy up to
`FEED_LEFTOVER_STALE_S`, 120 s, may be served). For a row with no baseline (`catalyst_price == 0.0`, always the case for Tier-3
volume-shock rows) it stored that tick's price as the baseline FIRST, and only later did `_watchlist_adverse_reason` check the tick's
age (`WATCHLIST_MAX_TICK_AGE_S`). So an old copy, or a tick whose age is unknown (`age_known False`: `as_of` is only the time we
received it), became the baseline; `pct_move` was then exactly 0.00% and a Tier-3 row was queued in the same cycle, on a price nobody
had seen move. A stock with no trade today (feed volume 0) has the previous close as its price and passed the same way.

## Change (`_watchlist_stale_price_reason`, called before the baseline is stored)
- Baseline only from a live price: a tick older than `WATCHLIST_MAX_TICK_AGE_S`, a `stale_last_good(...)` copy, or a tick of unknown
  age cannot set a row's baseline. The row waits for the next cycle. (`WATCHLIST_BASELINE_AGE_UNKNOWN=allow` accepts unknown-age
  baselines again.)
- No trade today: feed volume 0 and the price equal to the previous close (or no previous close) holds the row, with or without a
  baseline. Unknown volume never blocks; volume 0 with a price away from the previous close is treated as a feed glitch and passes.
- Held rows count in the tally as `stale_price` (key only present when > 0) and show in the existing throttled `SKIPPED` summary line
  with the reason.
- Rows that already have a baseline keep the old age path (`_watchlist_adverse_reason`), unchanged.

## Switches (`.env.example`, `.env.oracle.recommended`, `docs/ENV_REFERENCE.md`)
`WATCHLIST_STALE_PRICE_GATE` (1; 0 = off), `WATCHLIST_BASELINE_AGE_UNKNOWN` (refuse | allow), `WATCHLIST_NO_TRADE_HOLD` (1; 0 = off).

## Trade-off
If market-data ever stops sending `age_s`, every new row waits (rows show `baseline price age unknown` in the SKIPPED line) until
you set `WATCHLIST_BASELINE_AGE_UNKNOWN=allow`. Entries are still re-priced and age-checked by the entry engine before any order
(`ENTRY_MAX_QUOTE_AGE_S`), so this gate only stops stale rows reaching the queue.

## Tests
`tests/test_group360_stale_price_gate.py`: 15 helper cases (baseline from old / last-good / unknown-age tick, allow switch, blank
switch, existing baseline untouched, volume 0 at / away from previous close, unknown volume, switches, broken tick) and 3 trigger
cases on SQLite (old tick stores no baseline, no-trade stock not queued, live tick still baselines and queues Tier 3).

## Not verified here
The sandbox has no pytest / sqlalchemy: the 15 helper cases ran against extracted code; the 3 SQLite trigger cases and the existing
suites are unrun. Run `bash run_tests.sh` in real-trade-service.

## Still open
Item 2 (Oracle latency), and the remaining items of the 14 Oct list (5-9, 11-19) - see the list in the reply.
