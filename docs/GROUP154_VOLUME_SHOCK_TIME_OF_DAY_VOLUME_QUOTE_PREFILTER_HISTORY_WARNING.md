# group154 (2026-10-05) - volume-shock: time-of-day volume, quote pre-check, history-missing warning

Cumulative on group153. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: item 5 of the 2026-10-05 market-open VM log list ("volume-shock ratio is not time-of-day adjusted, and history returns None for most symbols"). Only `real-trade-service/candidate_engine/candidates.py` changed.

## 1. Time-of-day volume ratio (`_volume_shock_analysis`)
**Cause.** The 1mo/1d history's last candle is today's PARTIAL session while the market is open, but it was divided by a 20-day average of FULL sessions and compared with a 1.5x threshold calibrated on full-day data. At 10:00 IST about a fifth of a normal day has traded, so a stock already running at 2x its usual full-day volume showed ~0.4x and was rejected: the track was blind in the morning, when breakouts happen.
**Fix.** The partial volume is projected to a full session with an intraday cumulative-volume curve (`_SESSION_VOLUME_CURVE`, linear between points, 1.0 outside 09:15-15:30 IST, floored at `VOLUME_SHOCK_TOD_MIN_FRACTION` = 0.15 so the first minutes cannot inflate a few ticks into a "shock"). The adjustment applies only when the last candle's date is today. The result carries `vol_multiple` (projected), `vol_multiple_raw`, `tod_fraction`; a rejection reason shows both ("1.0x 20-day average (raw 0.2x, 20% of the session elapsed)").
`VOLUME_SHOCK_TOD_ADJUST=0` restores the raw partial ratio.
**Caveat.** The curve is an APPROXIMATE typical NSE intraday shape (heavy open, quiet midday, closing surge), not measured from this system's data. The 1.5x multiplier is unchanged; if the morning now lets too many through, raise `CANDIDATE_VOLUME_SHOCK_MULTIPLIER` or `VOLUME_SHOCK_TOD_MIN_FRACTION` rather than switching it off.

## 2. Quote pre-check before the history call
**Cause.** Every mover got a quote AND a daily-history request in parallel. Most fail the return gate, and the history call (one AngelOne getCandleData per symbol, shared rate bucket, shed under load) is what came back empty ("Insufficient daily history").
**Fix.** The quote is fetched first. If its live return (price vs `previous_close`) is below `VOLUME_SHOCK_MIN_RETURN_PCT` minus `CANDIDATE_VOLUME_SHOCK_PREFILTER_MARGIN_PCT` (1.0 point), the symbol is rejected without a history request. A quote with no usable price/previous_close never blocks the history call. `CANDIDATE_VOLUME_SHOCK_QUOTE_PREFILTER=0` restores the parallel fetch behaviour (history always requested).
**Trade-off.** Quote and history are now sequential per symbol (one extra round trip of latency for symbols that pass the pre-check), in exchange for far fewer history calls.

## 3. One WARNING per cycle when history was unavailable
`_refresh_volume_shock_candidates` counts "Insufficient daily history" rejections and logs one WARNING per cycle (`volume_shock: daily history unavailable for N of M symbol(s) ...`). Those symbols were skipped, not judged. Previously this only showed up as per-symbol INFO lines.

## Tests
`real-trade-service/tests/test_volume_shock_tod_and_prefilter.py` (21): curve shape / floor / outside-session, today-only and off-switch behaviour, bad clock, morning projection passes a real shock and still rejects a quiet stock, after-close and off-switch use the raw ratio, flat mover rejected without a history call, margin keeps near-gate symbols, unusable quote never blocks history, pre-check off switch, exception handling, and the per-cycle warning (fires once with the right count; silent when history was fine). All error out on the group153 code. `candidate_engine/candidates.py` stays at 100% coverage.

## Not changed
- The standard (`_multi_tf_analysis`) track's volume-health ratio.
- Why history is empty in the first place (AngelOne candle bucket shedding) is not fixed here; this group sends far fewer requests into it and makes the failure visible.
