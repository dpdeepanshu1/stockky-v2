# Group 230 - log review items 1-5 and 7 (history cache, feed cycle, tier 2/3 guard, exit timing, gate note)

Item 6 (Tier 1 queues falling stocks and misses big risers) is NOT changed: it needs a decision, see the end.

## Items 1 and 2 - /history cache and last-good candles (market-data-service/main.py)
Problem: daily candles of completed sessions never change, but the 1d/1mo series was cached only 900 s while the
market was open. The volume-shock list (128 symbols) plus candidate checks re-asked AngelOne `getCandleData` for the
same series every 15 minutes (8 candle 403 trips in about 15 minutes). When every source failed, the empty answer was
read by the candidate engine as "Insufficient daily history" (ASIANPAINT, INFY, HCLTECH ...).
- `_history_ttl(interval)`: 1d/1wk/1mo stay cached `HISTORY_DAILY_OPEN_TTL_S` (default 3600, floor 60) while open; intraday 900; closed 21600 (unchanged).
- Last good DAILY result is kept in the durable KV store (survives restart), saved at most once per `HISTORY_LAST_GOOD_SAVE_EVERY_S` (21600) per key,
  usable for `HISTORY_LAST_GOOD_MAX_AGE_S` (345600 = 4 days). Served flagged `stale=true`, `stale_age_s`, `source=last_good`.
- Served when (a) every source failed, or (b) the AngelOne candle-family cooldown runs and the request is not `force` (yfinance is not hit at all).
  Stale answers are re-cached for only 120 s so a real fetch replaces them. No last-good = the old error path, unchanged.
- `HISTORY_LAST_GOOD=0` turns the last-good part off.

## Item 3 - feed poll cycle (market-data-service/angelone_ws_feed.py)
Problem: 14 batches walked strictly one after another; cycles took 15-25 s (70 s at boot) against the 15 s target, and one `ConnectError` left a batch stale.
- First batch (held positions) still goes alone, so a rate-limit answer on it ends the cycle at once; the rest go `ANGELONE_FEED_BATCH_CONCURRENCY` (default 2) at a time. `1` restores the old walk.
- A batch that fails with `ConnectError`/`ConnectTimeout` is retried once after 0.5 s (not during a cooldown).
- The quote limiter (5/s) and lane budget still pace the calls. Log text for failed batches is unchanged.

## Item 5 - Tier 2/3 queued with no previous close (real-trade-service entry_engine/entry.py)
- A Tier 3 row with an unknown day change, or a Tier 2 row whose baseline was set from this very tick with an unknown day change, is held (stays active, re-checked next cycle).
  Tier 1 and Tier 2 rows with a known catalyst price are unaffected. `WATCHLIST_REQUIRE_PREV_CLOSE=0` restores the old fail-open behaviour.
- Test change: `test_tier3_without_prev_close_fails_open` now asserts the row is held; `test_tier3_zero_catalyst_price_also_queues_same_cycle` gives its tick a previous close.

## Item 4 - exit tick timing (real-trade-service execution/auto_pilot.py)
- Observation only: one WARNING (max once per 60 s per mode) when exit evaluation + reconcile exceed `EXIT_TICK_WARN_S` (default 15, 0 = off), with the split.
  The tick is never cancelled (cancelling in the middle of an order call is unsafe). This gives the data to decide whether a deadline is needed.

## Item 7 - gate log note (real-trade-service adaptive_thresholds.py, entry_engine/entry.py)
- `threshold_age_note(name, effective=None)`: when the applied gate differs from the table value it prints `(static default 25, set ..., Nd ago)`
  instead of a second `gate=`. Both call sites in entry.py pass `effective=threshold`.

## Candidate log (real-trade-service candidate_engine/candidates.py)
- "Insufficient daily history" reject lines now append `[history missing|unavailable: <reason> - no data on record|not judged]`.

## Tests added
market-data-service: `test_group230_history_last_good.py`, `test_group230_feed_batch_concurrency.py`.
real-trade-service: `test_group230_exit_tick_timing.py`, additions in `test_adaptive_thresholds.py` and `test_group155_watchlist_adverse_guard.py`.
The sandbox had no pytest/fastapi/httpx/sqlalchemy: py_compile on every touched file, plus the pure logic (history TTL and last-good helpers,
exit-tick timing, gate note, watchlist guard) and the batch scheduler were run standalone. Run `bash run_tests.sh` on the VM.

## Open - item 6 (needs your decision)
Tier 1 rows queue stocks that are falling (limit -3% vs catalyst) and miss big risers (band). Not changed.
