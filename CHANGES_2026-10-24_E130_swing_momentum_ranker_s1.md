# E130: swing-hold plan step S1, the read-only momentum ranker (Real Trade only)

Plan: `Stockky_Swing_Momentum_Plan.md` (section 7, step S1). **Read-only: no order, no gate, no switch, no database write, nothing reads the result to decide.** Trading behaviour is unchanged.

| New file | What |
| --- | --- |
| `services/real-trade-service/swing/momentum.py` | Pure measures on daily bars: 12-1 and 6-1 return (skip month = 21 days), daily volatility, momentum ratio (return / volatility, the NSE Momentum 30 idea), 200-day average trend, distance from the 52-week high, Wilder ADX(14), strength vs a benchmark; `rank_scores` (z-score of the ratios, NSE normalised score, rank, percentile, informational `screen`) |
| `services/real-trade-service/swing/ranker.py` | `build_snapshot(symbols)`: fetches `GET /history/{sym}?period=2y&interval=1d` from market-data (that service keeps at most 260 daily rows, enough for the 253 closes 12-1 needs), bounded workers and timeouts, failures listed, never raises |
| `services/real-trade-service/main.py` | One admin-only route `GET /swing/rank?symbols=A,B,C&benchmark=&limit=30&screen_only=false` (blocking fetch runs in a thread). `SWING_RANK_DEFAULT_SYMBOLS` is used when no symbols are passed |
| `services/real-trade-service/tests/test_swing_momentum.py` | 16 tests, run as pytest or `python3 tests/test_swing_momentum.py` |

Status per symbol: `ok` (253+ closes), `limited` (127+ closes, ranked on 6-1 only), `insufficient`. A perfectly flat series has no volatility and is not ranked. Fewer than 5 rankable symbols: nothing is ranked and the snapshot says so.

Checked here: the 16 tests pass as a plain script; 5 deliberate breakages (skip month, normalised-score shape both ways, +DM rule, the uptrend condition in `screen`) are each caught; `py_compile` on `main.py`; repo guards (shared copies, control catalog, secrets, merge markers) pass. **Not run here:** pytest itself and the real-trade suite (no pytest or FastAPI here), the route against a live market-data service, the Docker build.

Env knobs (all optional): `SWING_RANK_MAX_SYMBOLS` (200), `SWING_RANK_WORKERS` (4, max 8), `SWING_RANK_TIMEOUT_S` (8), `SWING_RANK_DEFAULT_SYMBOLS`.

Next (plan S2): `swing_backtest` on the daily-history replay with delivery costs at your swing size; it decides whether S3 (shadow lane) is built at all.
