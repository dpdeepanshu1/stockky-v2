# Group 479 (2026-10-21): position-stocks parity with groups 470-472, opening-range recorder, opt-in same-day broker stop

Why: the 21 Oct review asked for both services to get the loss-review guards. real-trade already had them (groups 470-474); position-stocks had
none of them. real-trade also had no resting stop for a same-day position (the broker stop covered earlier-day CNC holdings only, and with
overnight carry off that is almost nothing).

## position-stocks-service
| Change | Setting | Default | Rollback |
|---|---|---|---|
| Entry cut-off, every day | `SCALP_ENTRY_CUTOFF_IST` | **14:00** (was blank) | `23:59` (blank now means "use the default") |
| Whole-day stop-out pause | `STOPOUT_GUARD_ENABLED`, `STOPOUT_DAY_PAUSE_N` | true, **3** | `STOPOUT_GUARD_ENABLED=false` / `STOPOUT_DAY_PAUSE_N=0` |
| No same-day re-buy of a stopped-out symbol | `STOPOUT_SYMBOL_BLOCK` | true | `false` |
| Minimum shares per automatic entry | `ENTRY_MIN_QTY` | **3** | `0` |
| Opening-range-breakout recorder (record only) | `ORB_MODE` | shadow | `off` |

* `orders/stopout_guard.py`: counts `scalp_positions` closed today with status STOP_HIT / STOP_BREACH_EXIT and realized_pnl < 0 (a restart does not reset it).
  Placed in `orders/entry.py` right after the group 392 opening pause, before the position-count check, so nothing is claimed. Skip reasons `STOPOUT_GUARD:PAUSED:...` / `STOPOUT_GUARD:SYMBOL:...`.
  Automatic path only. Fails open on any error.
* `ENTRY_MIN_QTY`: judged after EVERY sizing step (VIX, calendar, opening half-size, slot cap), just before the cost gate. Skip reason `MIN_QTY:qty=N<3:ltp=...`.
  The forced 1-share first live order is never gated. Expect fewer entries: with the slot cap (pool / 5 x 1.5) a stock above about a third of the slot is skipped,
  and inside the 09:15-09:45 half-size window the minimum bites harder. Set `ENTRY_MIN_QTY=2` if that is too tight after a week of data.
* `orders/orb_shadow.py`: the 5-minute range (09:15-09:20) is fixed from the first /quote seen 09:20-09:26 (fed by `feed/md_poller.ingest_rows`, so it exists for every polled
  symbol, not only candidates). At entry the verdict (price above range high and VWAP, spread, range width, limit = high + 0.10 %, stop = range low) is merged into
  `scalp_positions.flow_json["orb"]` (kept by `order_flow.parse`). Nothing is ordered or blocked. No live mode: needs 30+ would-enter outcomes first.
* `GET /review-fixes/status` (admin, read-only): effective settings, today's losing stop-outs and symbols, pause state, ORB counters, whether super orders are on.
* Calibration page: the five names above are in `control/catalog.py` (protected from auto-calibration, same as real-trade).
* position-stocks already had a broker-side stop (`USE_SUPER_ORDER` default true), the 3-loss `LOSS_BRAKE`, a 09:10 pre-open snapshot, an opening plan and the opening gate. Not changed.

## real-trade-service
* `BROKER_STOP_INTRADAY` (default **false**): `execution/broker_stop.py` also places the resting Dhan stop for a same-day INTRADAY (MIS) position, with product INTRADAY.
  Needs `ENTRY_PRODUCT_MODE=auto` (with the default `cnc` no position is INTRADAY, so the flag changes nothing). The stop is cancelled before every software SELL,
  exactly like the CNC one. NOT verified against a live Dhan account: switch both on, take ONE small position, read `/orders` and Dhan's order book, then decide.
  Same-day CNC positions are still skipped (CDSL, cannot be sold until T+1). Protected from auto-calibration.

## Not built (needs your decision or evidence)
* A live opening-range entry (needs 30+ would-enter outcomes from either recorder).
* Limit-at-touch entry for position-stocks (group 282's code is not in this lineage; entries are MARKET with the 0.25 % slippage pre-check).
* Risk per trade in real-trade (5 %): the Rs3,000 position cap binds first, so lowering it changes little; decide separately.
* Gap-down guard for held positions: only matters for overnight carry, which is off.

## Tests
`position-stocks-service/tests/test_group479_guards.py` (16: tally, pause, symbol block, fail-open, ORB verdicts, shipped defaults in a clean interpreter, wiring order),
`real-trade-service/tests/test_group479_broker_stop_intraday.py` (5). conftest turns the new position-stocks rules off for older tests.
