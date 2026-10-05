# Group 162 - scalp review: real fills in P&L, entry price guard, exchange day range, faster exits (position-stocks-service)

Source: the 2026-10-05 review of the day's 11 scalp trades. The dashboard showed +Rs105; the broker fills
showed about -Rs24 before charges. Almost all of the gap was one trade (UNITEDPOLY: dashboard entry 44.58,
broker fill 48.14). Rebuild position-stocks-service only.

## 1. P&L from real fills (orders/reconcile.py)
- `_entry_fill_from_row` / `_apply_entry_correction`: the stale scan-time `entry_price` is replaced by Dhan's real
  entry fill. It is accepted when the parent row is filled, OR has `filledQty > 0`, OR a `legDetails` ENTRY_LEG is
  filled. Before, the parent `orderStatus` had to read TRADED, which it may not while exit legs are pending.
  A price on a row that never filled is still ignored.
- Flat-SELL exits (STAGNATION_EXIT / EOD_SQUAREOFF / MANUAL_EXIT) computed P&L in `_reconcile_eod_pending`
  BEFORE the entry correction ran. The correction now runs first, off one super-order fetch that the main pass
  reuses (still one fetch per pass). A failed fetch falls back to the stored entry price, as before.
- TARGET/STOP legs: when the leg dict carries no traded-price key, the real SELL fill is taken from today's order
  book (own security id, TRADED, exact quantity, price within 10% of the trigger, EXACTLY ONE match). Otherwise the old
  chain (leg price -> parent price -> trigger price) applies unchanged.
- Telegram/log alert when the fill differs from the signal by more than `ENTRY_FILL_SLIPPAGE_ALERT_PCT` (1%).

## 2. Entry guards (orders/entry.py)
`_price_guard_reject`, run after the range gate, before capital is reserved (clean skip, symbol lock released,
logged as `PRICE_GUARD:...`): stale last tick (> `ENTRY_MAX_TICK_AGE_S` = 45 s), live price more than
`ENTRY_MAX_SLIPPAGE_PCT` (0.5%) above the signal, stock up more than `MAX_DAY_GAIN_PCT` (7%) vs previous close.
Each fails open on missing data; 0 disables it. The entry is still a MARKET order (not changed to a limit order).

## 3. Exchange day high/low (feed/ws_client.py, entry range gate, adaptive, screener)
The tick buffer holds ~65 minutes, so its "day high/low" was the last hour. The mode-2/3 frame carries the
exchange's open/high/low/prev-close (bytes 91-122); `get_day_stats` / `get_day_range` expose them and the range gate,
`adaptive._intraday_range_position` and the screener's range multiplier prefer them, falling back to the buffer.

## 4. Exits and levels
- `NO_FOLLOWTHROUGH_EXIT_*`: inside `run_stagnation_exit`, a position that has not reached +0.5% at its best within
  20 min is closed (status STAGNATION_EXIT). Shares the Stagnation toggle on the Pipeline tab.
- `STAGNATION_EXIT_MINUTES` 45 -> 30.
- `ADAPTIVE_BAR_ATR_ENABLED` default True (0.8-2% stop, target RR 1.8, 3.5% cap).
- `BREAKEVEN_TRIGGER_MAX_PCT` = 1.0: trigger = min(40% of target, 1%). Only for positions opened from now on, and only
  if the Breakeven toggle (DB, off by default) is on.

## 5. Scan windows
`DISABLED_SCAN_WINDOWS` default `1,15` (5m and 60m stay on). `DISABLED_SCAN_WINDOWS=none` re-enables all.

## Env knobs (all optional)
ENTRY_MAX_SLIPPAGE_PCT, ENTRY_MAX_TICK_AGE_S, MAX_DAY_GAIN_PCT, ENTRY_FILL_SLIPPAGE_ALERT_PCT,
NO_FOLLOWTHROUGH_EXIT_ENABLED / _MINUTES / _MIN_GAIN_PCT, STAGNATION_EXIT_MINUTES, BREAKEVEN_TRIGGER_MAX_PCT,
ADAPTIVE_BAR_ATR_ENABLED, DISABLED_SCAN_WINDOWS.

## Things to know
- Not changed: rows already closed today keep their stale P&L and the ledger total built from it (Dhan's order book
  only holds the current day, and rewriting booked ledger P&L automatically is not safe). Entry window (09:30-14:30)
  and the MARKET entry type are unchanged.
- Smaller stops mean larger rupee positions: sizing is risk / stop, so a 0.8% stop buys up to 2.5x the shares a 2% stop
  did, for the same rupee risk.
- The day-stats byte offsets follow the layout documented in `feed/ws_client.py`; confirm on the first live frame
  (`get_day_stats("SBIN")` should return today's real open/high/low/prev close).
- Check in the logs after the first live trades: "entry_price corrected" lines, `PRICE_GUARD:` skips, and
  "exit fill ... taken from today's order book".

Tests: +100 (2513 passed, 2 skipped). New `tests/test_scalp_review_20261005.py`; additions in test_entry.py,
test_eod_squareoff.py, test_reconcile.py, test_ws_client_loop.py. Older tests that assumed the old defaults now pin them.
