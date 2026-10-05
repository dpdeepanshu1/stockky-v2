# group155 (2026-10-05) - watchlist trigger: no falling knives, Tier-3 direction check

Cumulative on group154. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: items A3, A4, A5 of the log audit of the 2026-10-05 market-open VM log (04:44-04:50 UTC).
Files changed: `real-trade-service/entry_engine/entry.py`, `real-trade-service/market_feed/feed.py` (+ tests).

## What the log showed
`watchlist trigger[...]: QUEUED` for KMSUGAR -20.00%, GLOTTIS -9.72%, MOMSBELIEF -9.70%, BAJAJHCARE -9.68%, MEDICO -7.78%,
PASHUPATI -7.35%, CHALET -7.09%, HEROMOTOCO -5.63%, ZYDUSLIFE -6.17%, while NYKAA (+6.3%) and TURTLEMINT (+7.7%) were MISSED.

## Cause
1. **One-sided band (A3).** `evaluate_watchlist_entries` only compared `pct_move > entry_band_pct`. Any fall was "within band" and
   queued. (An existing test, `test_price_below_catalyst_is_within_band_and_queues`, pinned this on purpose with a -10% case: the
   idea was "a drop is not chasing". Fine for a small dip, wrong for a -20% move.)
2. **Tier 3 has no direction (A5).** `watchlist_engine/sources.py::_tier3_volume_shock` takes `/scan/universe`'s raw
   `momentum_movers` list (gainers AND losers; the log's "Tier 3 produced 106" equals "Momentum movers collected: 106"). The real
   volume-shock gate (`_volume_shock_analysis`, return >= +2.5%) lives only in the separate candidate track, not here.
3. **0.00% moves (A4) are by design, not a bug.** A Tier-3 row has `catalyst_price=0.0`; the first live tick becomes the baseline
   (`catalyst_price_source="live"`) and the row is queued in the same cycle, so move is exactly 0.00% (PRIVISCL, PAGEIND). That
   was a deliberate latency choice, but it meant Tier 3 never looked at direction at all. The guard in (2) below now covers it.

## Fix (entry.py)
Both guards only decide whether a row is QUEUED this cycle. The row stays `active` and is re-checked next cycle, and every later
gate (quality, MTF, risk, Gate 6) is unchanged.
- **Adverse-move guard:** `pct_move < -WATCHLIST_MAX_DROP_PCT` (default 0.03 = 3%) is not queued.
- **Tier-3 direction guard:** a `source_tier == 3` row is not queued while its day change vs previous close is below
  `WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT` (default +1.0, in percent). If the tick has no previous close the guard does nothing
  (fail-open, same as before).
- `WATCHLIST_ADVERSE_GUARD=0` turns both off. Blank/bad env values fall back to the defaults. Any exception inside the guard is
  logged and the row is queued as before.
- The tally returned by `evaluate_watchlist_entries` gains an `"adverse"` count. One INFO line `SKIPPED (not queued, stays active)`
  per symbol per 30 min (not every 180 s cycle).

## Fix (feed.py)
`Tick` gets an optional `prev_close` slot (default None) filled from `previous_close`/`prev_close` in the `/quote` answer and in
`/quotes/bulk` items via `_safe_prev_close()`. The `live_quotes` DB path does not carry it, so ticks from that path have
`prev_close=None` and the Tier-3 direction guard does nothing for them (the drop guard still works). Never used for sizing/orders.

## Tests
- New `tests/test_group155_watchlist_adverse_guard.py` (13 tests): drop not queued and row stays active, small drop queued, env
  override, off switch, bad env, upward overrun still MISSED, Tier-3 down/up/no-prev-close, Tier 1 ignores the day-change rule,
  guard exception fails open, log throttle, `_safe_prev_close` / bulk tick parsing.
- `tests/test_watchlist_trigger.py`: tally dicts gain `"adverse": 0`; the -10% "still queues" test now uses -2%.
- real-trade suite (3 modules excluded as in earlier groups): 2772 passed, 2 skipped, 4 failed. The 4 failures are
  `test_oracle_compat.py::TestBuildOracleEngine` and fail identically on the uploaded group154 zip in this sandbox
  (no Oracle driver), so they are not caused by this change.

## Judgement calls to check
- 3% and +1.0% are my picks, not measured. Look at `SKIPPED` lines for a few sessions; if good setups are being held back, raise
  `WATCHLIST_MAX_DROP_PCT` (e.g. 0.05) or lower the Tier-3 minimum.
- Tier-1 "results"/"board" rows are judged only on price vs catalyst. There is still no sentiment-direction check for them.

## Not changed (still open from the audit)
A1/A2 (priority quotes timing out, AngelOne rate bucket), A6/A7 (quality-gate fail-open, capital check order), B1-B10, C1-C9.
