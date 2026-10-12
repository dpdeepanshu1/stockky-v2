# Group 334 - liquidity floor on the entry path (2026-10-12)

**Why:** before this the only liquidity checks before a BUY were the Rs 20 price floor, the market-cap floor, the best-5 book check and (real-trade) risk_engine's Rs 50 lakh traded-value floor. Nothing asked whether the name normally trades, or whether it is trading near that today.

**Files:** `position-stocks-service/orders/liquidity_gate.py` (called in `orders/entry.py` before the Market Board guard, before capital is reserved; skip reason logged as `LIQUIDITY_*`), `real-trade-service/entry_engine/liquidity_gate.py` (Gate 4d in `entry_engine/entry.py`, gate_tag `liquidity`). Both `prev_day.PrevDay` gain `avg_volume` and `avg_turnover` (mean over up to 20 completed sessions, needs 5 with a volume).

| Test | Rule | Default | Reason tag |
|---|---|---|---|
| Average turnover | normal daily turnover >= floor | Rs 3 cr (`ENTRY_MIN_AVG_TURNOVER_CR`) | `LIQUIDITY_AVG_TURNOVER` |
| Day turnover | today's volume x price >= floor, from `ENTRY_LIQ_MIN_TIME_IST` | Rs 1 cr, 10:00 | `LIQUIDITY_DAY_TURNOVER` |
| Relative volume | day volume / (normal volume x elapsed share) >= `ENTRY_MIN_RVOL`, from `ENTRY_RVOL_MIN_TIME_IST` | 0.35, 09:45 | `LIQUIDITY_RVOL` |

Elapsed share is front-loaded: (minutes since 09:15 / 375) ** 0.6, because a session's volume is not linear. A floor of 0 turns that test off; `ENTRY_LIQUIDITY_GATE_ENABLED=0/false` turns the gate off.

**Fail open:** missing / zero / NaN volume, no previous-day facts yet (a cache miss starts a background fetch and counts as unknown that cycle), no fresh quote, or any error = no opinion.

**Not verified offline:** that market-data `/history` candles carry `volume` for every symbol and that the live tick / `/quote` row `volume` is the cumulative day volume for the source in use. Where either is missing the test is simply skipped. Check Monday's candidate log / WAIT lines for `LIQUIDITY_` reasons and tune the three floors if too many liquid names are refused.

**Tests:** `tests/test_group333_liquidity_gate.py` in both services. conftest switches the gate off for older suites (it reads the clock and the prev-day cache).

**Still open from the group 332 request list:** expiry / Friday / pre-holiday rules, real NSE pre-open board, catalyst check, window auto-disable + width tuning.
