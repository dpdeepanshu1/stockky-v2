# Group 329 - circuit-band gate, results-day gate, spread re-check, weekend carry bar

Both trading services (backend only, no frontend change). Every new check FAILS OPEN: missing or unreadable data never blocks a trade.

## What changed
| # | Change | real-trade-service | position-stocks-service |
|---|---|---|---|
| 1 | **Circuit band**: no BUY within 1.0 % of the upper circuit or 2.0 % of the lower circuit (`CIRCUIT_UPPER_BUFFER_PCT`, `CIRCUIT_LOWER_BUFFER_PCT`, `CIRCUIT_GATE_ENABLED`) | `entry_engine/circuit_gate.py`, Gate 4b in `entry.py`; `Tick.upper_circuit/lower_circuit` read from the /quote row | `orders/circuit_gate.py`, called in `attempt_entry` after the depth gate; reads the same cached /quote row |
| 2 | **Results day**: no BUY on a stock whose results are dated today (`EARNINGS_DAY_BLOCK`) | `entry_engine/event_risk.py`, Gate 5.7 in `entry.py` (cached once per symbol per day) | `QualitySignal.earnings_date` / `passes()` in `screening/quality_gate.py` (same event-service answer the gate already fetched) |
| 3 | **No overnight carry into results**: today or the next trading day (`EARNINGS_CARRY_BLOCK`) | `_select_overnight_holds` in `execution/auto_pilot.py` | n/a (intraday only) |
| 4 | **Spread / book re-check at BUY time** on the fresh tick (`ENTRY_DEPTH_RECHECK`; limits `WATCHLIST_MAX_SPREAD_PCT`, `WATCHLIST_MIN_BOOK_VALUE`) | Gate 4c in `entry.py` | already had `depth_gate` |
| 5 | **Weekend / holiday carry bar**: `OVERNIGHT_HOLD_MIN_GAIN_PCT` + 0.5 % per extra closed day (`OVERNIGHT_HOLD_EXTRA_GAIN_PER_CLOSED_DAY_PCT`, 0 = off) | `_carry_gap_gain_ok` in `auto_pilot.py` | n/a |

Results date source: analysis-intelligence-service `GET /events/{symbol}/categorized`, items with `type == "earnings_date"` in `upcoming` **or** `recent`
(the endpoint files a bare date of today under `recent`). The date itself comes from yfinance, which is sometimes an estimate: a wrong date only costs a
skipped trade, a missing date changes nothing.

## Deliberately NOT done
* **Combined cross-service daily-loss cap.** position-stocks `capital/ledger.py` records that it was removed on purpose ("per explicit user decision";
  the two services trip their own kill switch). Peer P&L is still synced and shown, informational only. Left as is.

## Not built (needs a data source that could not be verified offline) - see the reply for the list.

## Tests
`real-trade-service/tests/test_group329_circuit_earnings.py`, `test_group329_depth_weekend.py` (incl. end-to-end through `evaluate_mode`),
`position-stocks-service/tests/test_group329_circuit_earnings.py`, `TestCircuitGateWiring` in `tests/test_entry.py`.
`tests/conftest.py` (real-trade) switches the results gates and the weekend bar off for older tests (network / weekday dependent).
Results: real-trade 4300 passed / 2 skipped, position-stocks 3439 passed. Not run against live market data.
