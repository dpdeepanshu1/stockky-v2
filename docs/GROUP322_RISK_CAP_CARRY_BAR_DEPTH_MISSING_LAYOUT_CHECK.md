# Group 322 (2026-10-11) - risk cap, overnight carry bar, depth "missing" policy, depth20 / order-WS layout self-check, optional broker-side stop, auto board gate, strategy readout

Cumulative on group 321. The first two items were written in the cut-off session and were not in the uploaded zip; they are redone here.

## 1. A trade may not risk more than the day's loss budget (real-trade `risk_engine/engine.py`)
Defaults were 5 % per trade against 3 % per day, so one full stop-out could breach the daily limit. The effective per-trade risk % is
now `min(per_trade %, max_daily_loss %)` (works with the conviction-adjusted `adj_risk_pct` too). `RISK_CAP_TRADE_RISK_AT_DAILY_LOSS=false`
restores the old behaviour; a daily-loss % of 0 / unset never zeroes the cap. Harmless today while the Rs 3,000 trade cap binds first.

## 2. Overnight carry needs a minimum gain (real-trade `execution/auto_pilot.py`, `config.py`)
A carried CNC position pays about 0.7 % round trip (DP fee + delivery STT on a Rs 3,000 trade) against about 0.1 % intraday, and takes the gap risk.
`OVERNIGHT_HOLD_MIN_GAIN_PCT` (default 1.5, 0 = off) is a bar on top of the existing net-of-costs check; below it the position squares off at EOD as before.
Unreadable entry price fails closed. The comparison is rounded (101.5 / 100 - 1 is 1.4999999999999 in floats and used to miss an exact 1.5 % bar).

## 3. Depth gate: what to do when there is no depth reading (position-stocks `orders/depth_gate.py`)
The spread / thin-book gate used to allow everything it could not read, so it protected nothing until depth was flowing.
`ENTRY_DEPTH_MISSING`: `auto` (default) = allow until a real spread / book reading has been seen, then refuse a name that has none
(`DEPTH_MISSING:...` skip reason, also when market-data is unreachable); `allow` = old behaviour; `refuse` = never enter without a reading.
`ENTRY_DEPTH_PROVEN_WINDOW_S` (21600) = how long a seen reading counts as "flowing" (0 = whole process). Set `ENTRY_DEPTH_MISSING=allow` if too
many scalps are skipped for `DEPTH_MISSING`. Tests reset the flag via `tests/conftest.py`.

## 4. Depth20 and order-WebSocket layout self-check (cannot be verified offline - these make Monday's check readable)
- market-data `dhan_data/depth20.py`: a book side whose prices are not a sane book (bids rising, asks falling, non-positive / NaN) is dropped and
  counted (`suspect_packets`); a crossed / locked book is not served (`crossed_books`, answer stays `warming`) - so a wrong byte layout cannot size an order.
  `GET /internal/dhan-status` (depth20 section) now shows `frame_samples` (first 3 raw frame heads as hex + any bad / unknown-code frame, no token)
  and `layout_verdict`: `unverified` (nothing yet) / `ok` / `suspect`.
- position-stocks `execution/order_ws.py`: `GET /orders/ws-status` now shows `samples` (first 3 messages + last unparsed ones, text cut to 400 chars,
  login message never sampled), `unparsed` and `layout_verdict` (`suspect` needs >= 5 unparsed with no event, or more than 1 unparsed per 4 events).

## 5. Housekeeping
- Silent `except Exception: pass`: 476 in production code, but the trading services' money paths have none left: the ones in real-trade / position-stocks
  are `db.rollback()` after the real error was logged, dashboard progress notes, token redaction, boot forensics. Two guard tests pin the per-file count
  for the order / exit / sizing / broker files (`test_group322_no_new_silent_excepts.py`, both services). The other ~400 are api-gateway / market-data /
  kv_cache cleanup and were not touched.
- decision-prediction-service: its three test packages were run (training 50, prediction 62, decision 81 passed). Coverage is still thin (17 test files for ~15.9k lines).
- `docs/ENV_REFERENCE.md` regenerated; `.env.example` has the four new variables.

## Tests
real-trade `test_group322_risk_cap_carry_bar.py`, `test_group322_no_new_silent_excepts.py`; position-stocks `test_group322_depth_missing_policy.py`,
`test_group322_order_ws_layout_check.py`, `test_group322_board_gate_auto.py`, `test_group322_no_new_silent_excepts.py`; real-trade also `test_group322_broker_stop.py`; scripts `test_strategy_readout.py`; market-data `test_group322_depth20_layout_check.py`.
Full suites in the sandbox: real-trade 4229 passed / 1 skipped, position-stocks 3398 passed, market-data 1632 passed.

## 6. Broker-side stop (real-trade `execution/broker_stop.py`) - OFF by default, `BROKER_STOP_ENABLED=true` to try it
NOT verified against a live Dhan account. Try it with ONE small position and read Dhan's order book before relying on it.
- Every REAL CNC position held since an EARLIER day (settled in demat; `broker_imported` positions too) gets a resting Dhan `STOP_LOSS_MARKET` SELL
  (or `STOP_LOSS`, `BROKER_STOP_ORDER_TYPE`) at the software stop less `BROKER_STOP_BUFFER_PCT` (0.5 %), so the software stop (8 s poll) normally fires
  first and the broker order only covers a dead / blind service. Same-day CNC positions are skipped (CDSL: they cannot be sold until T+1) and so are
  INTRADAY ones. Dhan may refuse a CNC sell on holdings without eDIS / TPIN authorisation: that is logged as such (once per symbol per 15 min), never raised.
- It is an ordinary `TradeOrder` row (SELL, `exit_reason="broker_stop"`, `limit_price` = trigger): no DDL. `sync_stops` runs from `evaluate_mode`
  (REAL only, at most every `BROKER_STOP_SYNC_SECONDS`), never lowers a stop, replaces it (cancel + place) when the stop ratchets up by `BROKER_STOP_MIN_MOVE_PCT`
  or the open quantity changed.
- `exit._send_real_sell` calls `cancel_for_exit` before ANY software SELL: no stop -> go; cancelled -> go; Dhan says it already traded -> the software SELL is
  skipped and reconcile books the fill; cancel failing for another reason -> wait, and after `BROKER_STOP_CANCEL_MAX_FAILS` (3) sell anyway (a second SELL on a
  holding that is gone is rejected by Dhan as an oversell, which the exit engine already handles). Cancelling works even after the flag is turned off.
- Coupling kept out on purpose: `_has_pending_real_sell` and reconcile's orphan repair ignore `broker_stop` rows (a resting stop must not switch the software
  exits off), and a dead stop in reconcile (cancelled / rejected / expired) gets its status only: no position restore, no failure streak, no backoff, no alert.
  A TRADED stop is booked like any SELL and closes the position.
- `dhan_client.place_order` gained `trigger_price` (only sent for STOP_LOSS types; every MARKET / LIMIT call builds the same SDK request as before).

## 7. Strategy settings
- `BOARD_GATE_MODE` default is now `auto` (position-stocks): shadow until the shadow report's written rule for the chase guard says "promote"
  (`BOARD_PROMOTE_MIN_OBS` 30 over `BOARD_PROMOTE_MIN_SESSIONS` 10, bootstrap CI), then enforce; re-checked every `BOARD_AUTO_REFRESH_S` and back to shadow if the
  verdict stops holding; unreadable verdict keeps the last answer (shadow before the first). `GET /board/shadow-report` shows `effective_gate_mode`.
  `enforce` / `shadow` / `off` still work as before.
- `scripts/strategy_readout.py` reads your own `/trades/report?days=14`, `/positions/REAL/breakdown` and `/board/shadow-report` and prints, per service, the
  expectancy of entries before 10:30 against after (`SET OPENING_ENTRY_NOT_BEFORE_IST=10:30` / `KEEP THE CURRENT WINDOW` / `NOT ENOUGH DATA`, minimum 30 trades each side;
  a bucket straddling the cutoff is left out). The window default stays 09:15 (group 268 lowered it on 14-day data) until that readout says otherwise.
- `ENTRY_LIMIT_AT_TOUCH` does not exist anywhere in the code (it was only a suggestion); entries are not changed.

## NOT done
- Live check of the broker stop, Depth20 and order-WS frames (Monday 09:30 IST: `curl /depth/RELIANCE`, `layout_verdict` in `/internal/dhan-status`, `/orders/ws-status`).
- No-entries-before-10:30 is a decision for `strategy_readout.py` on your numbers, not a code change.
- 2027 NSE holidays (NSE publishes the list in December), api-gateway `main.py` split, frontend Market Board never viewed in a browser.
