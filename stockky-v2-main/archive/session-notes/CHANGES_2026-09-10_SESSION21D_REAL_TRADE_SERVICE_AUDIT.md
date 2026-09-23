# Session 21d — real-trade-service audit (2026-09-10)

Follow-up audit focused specifically on `services/real-trade-service`, on
top of session 21c's EOD-squareoff/self-heal fixes already in this zip.
Read through execution/dhan_client.py, execution/auto_pilot.py,
execution/reconcile.py, execution/equity_sync.py, portfolio/portfolio.py,
exit_engine/exit.py, entry_engine/entry.py, risk_engine/engine.py,
manual_engine.py, cycle_runner.py, auth/dhan_credentials.py, models.py,
db.py, market_feed/feed.py, and the real-trade routes in main.py.

Four bugs found and fixed, all real-money-relevant. Everything else
reviewed held up — tick-size math, fill-delta booking, ghost-position
reconciliation, risk_engine's 9 checks, and the pyramiding/status-set
fixes from earlier sessions are internally consistent.

## Fix 1 — manual MARKET BUY sent a nonzero price to Dhan

`manual_engine.py`'s REAL BUY path called `dhan_client.place_order(...,
order_type=order_type, price=reference_price, ...)` unconditionally —
for a manual ticket with `order_type="MARKET"`, this sent Dhan's own
current tick price alongside a MARKET order. Every other MARKET order in
this codebase (every `_send_real_sell` call) correctly sends `price=0`
per `dhan_client.place_order`'s own module comment ("MARKET orders
correctly send price=0 (no limit) — never touch that"). entry_engine
never hit this because it only ever places LIMIT orders
(`config.ENTRY_ORDER_TYPE`), so this path — a manual "Buy at Market"
ticket — was the one place a stray nonzero price could reach Dhan on a
MARKET order, risking an outright rejection or being misread as a price
cap. Fixed: `order_price = 0 if order_type == "MARKET" else
reference_price`.

## Fix 2 & 3 — manual HTTP routes never actually got the event-loop-isolation fix

`execution/auto_pilot.py`'s module docstring (point 4) documents moving
every tick's body onto a worker thread with its own event loop so a slow
cycle (remote-Oracle round trips across many open positions) can't block
`/health` and other concurrent requests — the exact symptom decision #28
diagnosed. `main.py`'s `/cycle/run/{mode}` docstring already *claimed*
"the main event loop is never blocked... same as the auto-pilot tick
functions use" — but the code underneath never did that: `import asyncio`
was unused, and `run_cycle_core` was awaited directly on the shared main
loop, with only the per-mode lock acquisition actually fixed (decision
#32). `/manual-order/{mode}/confirm` had the identical gap. Both now wrap
the actual cycle/order-confirm body in `asyncio.to_thread`, same idiom as
`auto_pilot._run_coro_in_new_loop` — the `db` Session is safe to hand
across since the calling thread does nothing else with it until the
awaited call returns.

## Fix 4 — manual "Close Position" had no pending-SELL guard

`manual_engine.evaluate_manual_order`'s SELL path and
`auto_pilot._eod_squareoff` were both already fixed (session 21c) to
check `_has_pending_real_sell` before sending a SELL, because the
per-mode lock only rules out a *simultaneous* send — it doesn't rule out
an *earlier* tick's SELL that already finished sending (and released the
lock) but hasn't been broker-confirmed yet. `main.py`'s
`/positions/{mode}/{position_id}/close` route (the dashboard's manual
close button) was missing this same guard: a stop-hit SELL sent moments
earlier by the fast-exit tick, still awaiting Dhan fill confirmation,
left the position's `qty_open` un-decremented — a "Close Position" click
landing in that window would have fired a second, overlapping MARKET
SELL. Added the same `_has_pending_real_sell` check, refusing with a 409
if a SELL is already in flight, matching the manual-ticket SELL path
exactly.

## Reviewed, no changes needed

- `execution/dhan_client.py` — tick-size Decimal math, security-id cache,
  CDSL/oversell/intraday-cutoff/exchange-not-allowed error detectors.
- `execution/reconcile.py` — fill-delta booking, orphaned PENDING_EXIT
  repair, dead-order handling.
- `portfolio/portfolio.py` — `import_broker_holdings` /
  `holdings_sync_reconcile` ghost-position logic, `record_real_fill` /
  `record_real_exit_fill` / `force_close_real_position` cash & P&L
  bookkeeping. (`record_real_fill` doesn't itself refresh
  `account.current_equity` after a BUY fill, but `equity_sync.py`
  overwrites `cash_available`/`current_equity` from Dhan's own live
  balance before every risk decision/cycle for REAL, so this is not a
  live gap — confirmed by tracing both call sites.)
- `risk_engine/engine.py` — all 9 checks, SELL-side bypass list,
  conviction-adjusted sizing.
- `entry_engine/entry.py` — regime gate, drift gate, R:R floor, Gate 6
  composite ranking, `reserved_cash` accounting across a multi-candidate
  cycle.
- `auth/dhan_credentials.py` — token hard-cap clamping, TOTP refresh,
  IST-vs-UTC expiry parsing.
- `cycle_runner.py`, `market_feed/feed.py`, `db.py` migrations — all
  consistent with what the rest of the service reads/writes.
