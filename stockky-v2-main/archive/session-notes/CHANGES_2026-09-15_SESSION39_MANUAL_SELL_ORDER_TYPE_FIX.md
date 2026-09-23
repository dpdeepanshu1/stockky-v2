# Session 39 — manual SELL silently downgraded LIMIT to MARKET

**Reported:** DATAMATICS positions rejected repeatedly on the Dhan app with
"insufficient funds" on **Limit** sell orders (₹768.xx), plus a Stockky
Telegram feed showing repeated automatic `emergency_gap_down` SELL sends for
the same DATAMATICS position at ₹812.02 minutes apart.

## Diagnosis

1. The ₹768.xx LIMIT rejections in the Dhan Orders tab (60 failed) do **not**
   match any price Stockky was quoting for this position (₹812.02 entry) and
   no code path in this service can produce a LIMIT SELL at all — confirmed
   by grepping every `order_type` reference across `orders/`, `exit_engine/`,
   and `manual_engine.py` on the live server. Those were placed directly
   through the Dhan app (manual entry / repeated "Repeat Order" taps),
   outside Stockky. **Nothing to fix in this codebase for that part.**
2. While tracing the same code, found a real (if unrelated) bug:
   `evaluate_manual_order()` in `manual_engine.py` validates and prices
   `req.order_type` (LIMIT/MARKET) identically for BUY and SELL tickets, but
   only the BUY branch ever used it. The SELL branch called
   `exit_engine.exit._send_real_sell()`, which **always** hardcoded
   `order_type="MARKET", price=0` — so a manual Stockky "Sell" ticket that
   explicitly asked for a LIMIT order silently went out as MARKET every
   time, with no error and no indication to the user.

## Fix — `services/real-trade-service/exit_engine/exit.py`,
`services/real-trade-service/manual_engine.py`

- `_send_real_sell()` now takes optional `order_type` / `limit_price`
  keyword args (default `order_type="MARKET"`, `limit_price=None` — byte-
  for-byte the same effective behavior as before when omitted).
- **Every automatic call site is unchanged**: `emergency_gap_down`,
  `stop_hit`, `target_hit_partial`, `time_stop` (all in `exit.py`),
  `eod_squareoff` (`execution/auto_pilot.py`), and the dashboard
  "Close Position" button (`main.py`) all still omit both args, so they
  keep sending MARKET exits exactly as before — capital protection for an
  automatic exit is unaffected by this change.
- `manual_engine.py`'s manual-SELL confirm path now passes the ticket's own
  already-validated, already-tick-rounded `order_type` / `reference_price`
  through to `_send_real_sell()`, so a manual LIMIT sell actually places a
  LIMIT order at Dhan instead of being downgraded to MARKET.
- The `TradeOrder` row, its `TradeOrderEvent` detail, and the Telegram
  "SELL sent" alert now all record the order type/price that was actually
  sent, instead of an unconditional `"MARKET"` string that no longer
  matched what a manual LIMIT sell had done.
- A `LIMIT` request with no usable price still falls back to `MARKET`/`0`
  as a defense-in-depth floor inside `_send_real_sell()` itself (never
  places a `LIMIT` order at price `0`), even though `manual_engine.py`'s
  existing validation should already reject that case earlier.

## Not changed / still open

- The repeated `emergency_gap_down` "SELL sent" Telegram messages for the
  same position minutes apart are consistent with `_has_pending_real_sell()`
  correctly unblocking retries once `reconcile_real_orders()` marks an
  earlier attempt `REJECTED` — i.e. Stockky retrying an exit that keeps
  failing at the broker for an account-level reason (margin/funds), not a
  duplicate-send bug in the retry guard itself. Confirming that needs the
  live Dhan order-book detail (rejection reason per `dhan_order_id`) for
  those specific `emergency_gap_down` sends, which wasn't available this
  session — flagged for a follow-up session rather than guessed at here.
- Whether commits on `backup-production` beyond this zip's base
  (`session33c`) are already live on the server is still unconfirmed — diff
  against the actual last-deployed zip before overwriting.
