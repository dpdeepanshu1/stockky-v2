# Group 247 - 6-month and 1-year candidate views come from daily candles, not two weekly yfinance calls (Priority 4 of the 2026-10-08 log review)

From the 2026-10-08 open log: SUNFLAG, TRANSRAILL, KAVDEFENCE, DELTACORP, SUNTV, DYNAMATECH, AHCL and INDIAGLYCO got incomplete
timeframe history and were skipped as "cannot judge" (retried, not rejected as weak); TCS and TECHM got a history ReadTimeout.
Of the 7 `/history` calls per standard candidate, 4 go to yfinance (60m, 6mo/1wk, 1y/1wk, 2y/1mo), the bucket that saturates
under load. The other three daily ones (5d, 1mo, 3mo) plus a 1y/1d share one cached AngelOne fetch in market-data (group231).

## real-trade-service (candidate_engine/candidates.py)
- `_multi_tf_analysis` now asks for the 1y/1d series (the one market-data already holds) instead of the 6mo/1wk and 1y/1wk
  series, and cuts both views from it: the 1-year view is the whole series, the 6-month view is every bar within 183 days of the
  last bar. The checks that use them read only the first open and last close (return) and the highest high / lowest low
  (52-week range), and those are the same numbers from daily bars as from weekly bars (a weekly bar's open, close, high and low
  are made from its daily bars).
- Safety net: if the daily series is missing, an exception, shorter than `CANDIDATE_WEEKLY_FROM_DAILY_MIN_BARS` (default 60), or
  has missing / bad dates, the two weekly calls run exactly as before.
- Effect per standard candidate: yfinance calls 4 -> 2 (60m and 2y/1mo remain), and the daily series is shared with the
  existing AngelOne fetch, so no new AngelOne candle call. About 36 fewer yfinance calls per 18-candidate cycle.
- `CANDIDATE_WEEKLY_FROM_DAILY=0` restores the weekly calls.

## Not changed / limits
- 60m (intraday) and 2y/1mo stay on yfinance: the 2-year base needs more than the 1y daily series holds.
- A weekly bar from yfinance starts on the week's Monday while the daily cut starts on an exact date, so a 6-month return can
  differ by a few days of window, not by the method. Thresholds were not retuned.
- The volume-shock track and the quote/history gate from group 246 are unchanged.
- Not confirmed live. After the next open, "incomplete timeframe history" warnings and the `cannot judge` skips should drop.

## Tests
New `tests/test_group247_weekly_views_from_daily.py` (8): the cut (6-month window, full year, short listing), every refusal case,
no weekly call when the daily series is usable, weekly calls when it is missing / an exception / too short / switched off, and
that returns and the 52-week range come out the same. Real pytest: full real-trade-service suite 3660 passed, 1 skipped; the
`test_group172` teardown error and the other uncovered lines are also on the unmodified upload in this sandbox.
