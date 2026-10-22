# C18-C20 - pattern and indicator engine (2026-10-22), context only

`shared/market_structure/patterns.py` (vendored to every service as `stockky_ms/patterns.py` by `scripts/sync_market_structure.py`).
`analyze_extras()` now returns a `patterns` section, so `/structure/{sym}?extras=1` shows it. **No decision, gate, score or ML feature reads it.**

| Section | What it reports |
|---|---|
| `candles` | 16 patterns on the last 3 bars: doji, marubozu, hammer, inverted hammer, shooting star, hanging man, bullish / bearish engulfing, bullish / bearish harami, piercing line, dark cloud cover, morning / evening star, three white soldiers, three black crows. Reversal patterns need a > 1 ATR move over the 5 bars before them. Each carries `bias`, `bars_ago`, `volume_confirmed` (volume >= 1.2x its 20-bar average). |
| `chart` | 12 patterns from confirmed pivots (3 bars each side): double top / bottom, head and shoulders and inverse, ascending / descending / symmetrical triangle, rising / falling wedge, rectangle, bull / bear flag. `status` is `forming` or `confirmed` (close beyond the neckline / line / flag by 0.25 ATR); neckline, lines and a measured target where they apply. |
| `bollinger` | 20-bar band width, its percentile over the last 120 bars, `squeeze` (<= 10th percentile) and `release` (bullish / bearish: squeeze within the last 5 bars, now a close outside a band). Needs 140 bars. |
| `crosses` | Golden / death cross of SMA50/200 (needs 205 bars) and SMA20/50 (needs 55) in the last 5 bars, plus the current side. |
| `confluence` | MACD(12,26,9) + RSI(14): bullish = MACD above signal, histogram rising, RSI 50-70; bearish = the mirror (RSI 30-50). |
| `sr_flip` | Broken level that held on a retest from the other side (old resistance now support, or old support now resistance), up to 3, with `broken_bars_ago` / `retest_bars_ago`. |

No lookahead: a pivot exists only once 3 later bars exist (a test truncates a series at several points and compares). Every section is `None` with too few bars and a failing section
is isolated into `errors`; the call never raises. 16 new tests in `shared/market_structure/tests/test_patterns.py` (each bullish case is mirrored by flipping prices).

## Not done here (on purpose)
* C21 (adding the signals to `scoring.py` / `ml_features.py`): `ml_features` has a column hash that stored feature rows are compared against, so adding columns would orphan old rows and
  change what the ML sees. It waits until these signals have been read in the panel and have passed the B tools (`robustness.py`, `ma_baseline.py`, `live_vs_backtest.py`).
* Telegram alerts for squeezes / crosses / breakouts (item 29) and the chart drawing (item 25) are in E.
* Thresholds (0.1 doji body, 2x wick, 0.25 ATR confirmation, 10th percentile squeeze) are textbook starting values, not calibrated on your data. Treat the output as a label, not a signal.
