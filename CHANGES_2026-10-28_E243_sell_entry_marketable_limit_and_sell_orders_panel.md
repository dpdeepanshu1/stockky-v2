# E243 - SELL entry fills (marketable limit) + a SELL orders panel on the Positions tab (2026-10-28)
Roll back: restore the files listed under "Files" from the E242 zip. Set `SHORT_ENTRY_MARKETABLE_LIMIT=false` to get the old MARKET entry without touching code.

## What happened (real run, 10:14 IST, job dhan_sell_probe, NTPC)
Order accepted (id 34326102815775). Dhan's row, read back: `orderType LIMIT, price 300.4, filledQty 0, PENDING` - 300.40 is our reference (last) price. Positions flat, so the probe
cancelled and bought nothing (E242 did its job). Verdict FAIL: "the SELL entry did not show as filled within 20s".
The code sends `orderType MARKET` with NO price for a Super Order entry (dhan_client.place_super_order, SDK bypass), and Dhan stores it as a LIMIT at the reference price.
A SELL limit at the last price matches only a buyer at or above it; the best bid sits under it, so it rests. (The two cancel lines "TARGET_LEG ... Rejected / STOP_LOSS_LEG already
Cancelled" are normal: cancelling the unfilled entry already cancels the legs.)
NOT proven: why Dhan converts the MARKET to a LIMIT (market protection or the price-less payload). The fix below does not depend on the reason.

## Fix
- `dhan_client.short_entry_order(live, target, stop)` -> `("LIMIT", price)` a few ticks UNDER the last price (`SHORT_ENTRY_CUSHION_PCT`, default 0.3 %), kept only while target < price < stop and
  on a valid tick; otherwise the old `("MARKET", live)`. Never raises.
- `orders/short_live.py` (the live short) and `orders/sell_probe.py` (the probe) both send that entry, so the probe tests exactly what the live short sends.
- Switches (Switches page, group "Short selling"): `SHORT_ENTRY_MARKETABLE_LIMIT` (ships true), `SHORT_ENTRY_CUSHION_PCT` (ships 0.3). Both are money-safety flags a calibrator may never change.
- Stop and target legs are unchanged. The fill is at the best bid, never worse than the cushion; the cover and exit paths are untouched.

## New: SELL orders panel (Positions tab, top)
- `GET /short/orders` (position-stocks, read-only): the SELL probe runs (persisted to `sell_probe_history_ps.json`, last 50), the live shorts (side SHORT), and Dhan's own Super Order
  row for each order id Stockky placed (state WAITING / FILLED / PARTIAL / CANCELLED / REJECTED, entry order type and price, leg statuses).
- `SellOrdersCard` shows every row with a red SELL tag and a probe / short tag; a SELL that Dhan holds RESTING is called out in the headline. Polls every 10 s while the tab is open.
- New files: `orders/sell_orders.py`, `frontend/src/sellOrdersView.ts`, `frontend/src/components/board/SellOrdersCard.tsx`.

## Files
services/position-stocks-service: config.py, execution/dhan_client.py, orders/sell_probe.py, orders/short_live.py, orders/sell_orders.py (new), main.py, control/catalog.py,
tests/test_sell_probe.py, tests/test_short_entry_order.py (new), tests/test_sell_orders.py (new)
services/calibration-runner-service: autocalib.py
scripts/test_short_live.py
frontend: src/sellOrdersView.ts (new), src/components/board/SellOrdersCard.tsx (new), src/components/PositionStocksTab.tsx, src/positionStocksApi.ts, src/sellOrders.test.mjs (new), package.json

## Tests (what ran in the sandbox, which cannot install SQLAlchemy / npm packages)
- scripts/test_short_live.py (stand-ins): ALL 93 OK (live SELL is now a LIMIT under the last price).
- tests/test_short_entry_order.py: 6 pass. tests/test_sell_orders.py: 5 pass (no DB needed).
- frontend sellOrders.test.mjs: 5 pass; positionSide and shortShadow tests still pass.
- Scripted-Dhan simulation of the real sell_probe.py (bid 300.35, last 300.40): MARKET entry -> FAIL "never filled", no BUY; marketable-limit entry (299.50) -> PASS with one cover. The
  simulation encodes the "rests at the last price" theory, so it shows the new path behaves, not that Dhan behaves that way.
- NOT run here: tests/test_sell_probe.py (needs SQLAlchemy), `npm run build` / tsc. Please run on the VM:
  `cd services/position-stocks-service && pytest tests/test_sell_probe.py tests/test_short_entry_order.py tests/test_sell_orders.py tests/test_short_live.py -q`
  `cd frontend && npm run build && npm test`

## Next
Rebuild position-stocks-service and frontend, run the Dhan SELL probe again (market hours, 09:30-14:30). Read the "dhan" and "raw" lines: `orderType` on `row_after_fill` should be LIMIT at about
299.5 with filledQty 1. If it still rests, send the output; the next lever is a larger cushion (`SHORT_ENTRY_CUSHION_PCT` up to 1.0) or a plain MARKET SELL + BUY stop (E114 path).

# E244 (same day, 10:35 IST re-run) - the entry FILLED; two follow-ups
Result of the E243 fix on NTPC: `orderType LIMIT 299.75, filledQty 1, TRADED`, positions net -1, stop 305.15 / target 291.65 legs read back as BUY, covered at 300.7. The marketable limit works.
The probe still said FAIL: Dhan's row has `averageTradedPrice 0.0` and no ENTRY_LEG detail, so reconcile's price reader (`_entry_fill_from_row`) returned nothing and the probe took that for "not filled".
- `sell_probe._entry_filled`: a row with TRADED status or filledQty > 0 is a fill; price falls back to the position's sellAvg, else the limit sent, and a rule line says so. (Reconcile itself is unchanged:
  it reads quantity/status for the fill and keeps the scan price when Dhan gives no average, so a live short is tracked; only its entry price is the scan price, not Dhan's.)
- Panel was empty because (1) probe history only exists from E243 on, and (2) the 3-share ITC short from the 09:41 run was never a Stockky row. `/short/orders` now also returns Dhan's positions
  (shorts held at Dhan, flagged NOT IN STOCKKY when no row) and every SELL Super Order in Dhan's book (flagged when Stockky has no record). They list first, in red.
- **The 3-share ITC short is still open at Dhan (your screenshot). Nothing in Stockky manages it. Close it in Dhan.**
- Tests: tests/test_sell_orders.py 7 pass; frontend sellOrders.test.mjs 7 pass; scripted-Dhan run of the real probe with the NTPC row shape: PASS, one cover. New test
  `test_a_traded_row_with_no_average_price_is_a_fill_not_a_failure` in tests/test_sell_probe.py could not be run here (needs SQLAlchemy): run on the VM.

Also (E244): the probe's entry line now reads fill minus reference for a SELL (positive = sold higher = in our favour); it had the sign backwards. Log wording only.
