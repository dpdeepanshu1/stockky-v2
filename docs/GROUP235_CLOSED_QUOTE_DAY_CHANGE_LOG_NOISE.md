# Group 235 - closed-market quotes carry the real day change; bhavcopy hit log flood

Found in the 2026-10-07 evening boot log after group 234 was deployed.

## Verified working in that log (group 233/234/232)
- `bhavcopy prewarm: 2026-10-07 cached (2941 rows, 2941 with a close price)` - the CLOSE_PRICE parse fix works.
- Closed-market `/quote` and `/quotes/bulk` answered from bhavcopy; no AngelOne "lane budget shed" and no Yahoo
  call for any symbol that is in the bhavcopy.
- `POST /cycle/run/REAL` -> 409; after confirmation `?force=true` -> 200, with the "outside market hours" warning.
- Startup reconcile `REAL OK (3 open positions match snapshot)`; COHANCE re-import skipped by the settlement-lag guard.
- Not exercised in that log: the IndianAPI guard in `/analyze` (no market-data timeout happened), the stage deadline
  (no stage came near 60 s), the "Could not reach <path>" frontend message.

## 1. market-data-service - "0.0% today" on every closed-market bhavcopy quote
`_closed_last_close_row` built the bhavcopy row with `previous_close == close`, so every consumer computed a 0.0%
move. The log shows 229 volume-shock candidates rejected on "Today's return 0.0%", and Hot Picks / UI day-change
read 0 after hours. The bhavcopy line already has the real values.
- `_parse_bhav_csv_all` now also returns `prev_close` (PREV_CLOSE / PrvsClsgPric), `day_high`, `day_low`, `volume`.
- New `bhavcopy.eod_row_from_bhavcopy(symbol)` (newest row with a close, plus its session `date`);
  `eod_close_from_bhavcopy` delegates to it (same cache, same miss memory, same log lines).
- `_closed_last_close_row`: the price still comes from `_waterfall_bhavcopy_price`; previous_close,
  `day_change_pct`, `day_high`, `day_low`, `volume` are added from the same row only when its close equals that
  price. No PREV_CLOSE in the file -> the old shape (previous_close = close, day_change_pct None). Any error in the
  extras is swallowed and the plain quote is returned.

## 2. market-data-service - log flood
"Bhavcopy EOD waterfall hit X" was one INFO line per symbol per request (~1,500 lines in a few minutes). Now one
INFO per symbol and price per process (`_log_bhavcopy_hit_once`); repeats are DEBUG.

## Checked and NOT a problem
The 12-date miss walk for symbols that are in no bhavcopy (QUALIANCE, BMISL) does not evict the newest day: the
cache drops the oldest date first. It costs about 10 CSV downloads once per BHAVCOPY_EOD_MISS_TTL_S (6 h) per
such symbol.

## Tests
market-data tests/test_group235_closed_quote_day_change.py (10).
