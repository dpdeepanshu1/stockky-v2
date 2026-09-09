# 2026-09-01 — Conviction-sizing clawback fix + emergency-exit distance fix

## 1. Fixed: risk_engine silently nullified conviction-based position UPSIZING

**Files:** `services/real-trade-service/risk_engine/engine.py`,
`services/real-trade-service/entry_engine/entry.py`

entry_engine scales the per-trade risk % by conviction (±25% around the
65 midpoint) and sizes `proposed_qty` off that adjusted number — a
conviction-100 candidate is meant to get a bigger position than a normal
one. But the `OrderIntent` passed to `risk_engine.evaluate()` never carried
that adjusted percentage. risk_engine's own per-trade-cap check (#5)
independently re-derived the cap from the raw, un-adjusted
`account.risk_per_trade_pct` and silently downsized any upsize straight
back to the base amount. Downsizing (conviction < 65) was unaffected —
it already sat under the base cap.

Verified numerically before the fix: conviction=100, ₹1,000,000 equity,
1% base risk, ₹50/share risk → entry_engine proposed 250 shares (the
intended +25%), risk_engine clawed it back to 200 (the un-adjusted 1%
amount).

**Fix:** `OrderIntent` gained an optional `adj_risk_pct` field.
entry_engine now passes its computed `adj_risk_pct` through; risk_engine's
check #5 uses it when present, falling back to `account.risk_per_trade_pct`
otherwise (so manual orders, which don't set it, are unaffected and keep
working exactly as before). Verified after the fix: the same 250-share
order now passes through un-clawed (only the unrelated 25%-concentration
cap, which sits higher in this scenario, applies as before).

## 2. Fixed: gap-down emergency-exit threshold drifted over a position's life

**Files:** `services/real-trade-service/models.py`,
`services/real-trade-service/db.py`,
`services/real-trade-service/portfolio/portfolio.py`,
`services/real-trade-service/exit_engine/exit.py`

The emergency-exit check compared unrealized loss (from entry) against
`EMERGENCY_LOSS_MULT × "original stop distance"` — but that distance was
re-derived every cycle from `position.current_stop`, which itself moves
via breakeven/ATR-trail logic. Effect: once the trail tightens near LTP
the threshold shrinks toward zero (mostly harmless — just relabels an
ordinary stop-hit as "EMERGENCY" in the log); once breakeven pushes
`current_stop` above entry, the threshold grows, delaying the emergency
catch precisely when the position has the most unrealized profit at
stake — the opposite of the check's stated intent.

**Fix:** added `trade_positions.initial_stop_distance`, a nullable column
fixed once at position-OPEN time (`|fill_price − stop_price|` from the
opening fill, additive DB migration same idiom as the existing
`_ensure_manual_order_columns`/`_ensure_gate_state_columns`). exit_engine
now reads this fixed value for the emergency-gap check, falling back to
the old current_stop-based approximation only for positions opened before
this migration (which have no stored value).

## 3. Fixed: dead code + redundant floor knobs in risk_engine.py

**File:** `services/real-trade-service/risk_engine/engine.py`

`passes_hard_floor()` and `HARD_FLOOR_CONVICTION` (a conviction floor of
40) were defined but never called from `evaluate()` or anywhere else —
incomplete leftover wiring from the original "§5 hard floor" feature.
`HARD_FLOOR_PRICE` was also only ever read inside that dead function,
duplicating `MIN_STOCK_PRICE` (both defaulted to ₹20, only `MIN_STOCK_PRICE`
was actually enforced, in check #4a).

**Fix:** removed `passes_hard_floor()` and `HARD_FLOOR_CONVICTION` entirely
(candidate_engine already enforces its own, stricter `MIN_CONVICTION=55`
gate upstream, so nothing relied on this). Collapsed `HARD_FLOOR_PRICE`
into `MIN_STOCK_PRICE` — `RISK_MIN_STOCK_PRICE` env var takes priority,
falling back to `HARD_FLOOR_PRICE` (for anyone who had it set) before the
₹20 default, so no live behavior changes for anyone already configured,
but there's only one knob to tune going forward. `HARD_FLOOR_LIQUIDITY`
was left alone — it's genuinely used in check #4b.

## 4. Fixed: reconcile.py's missing-decision fallback now matches entry_engine's real constants

**File:** `services/real-trade-service/execution/reconcile.py`

If a broker fill couldn't be matched to its originating `TradeDecision`
row (or the row was missing stop/target), `_book_fill_delta` fell back to
a hardcoded 3%/3% stop/target — inconsistent with the 3.2%/6.5%
(`FLAT_STOP_PCT`/`FLAT_TARGET_PCT`) entry_engine actually uses everywhere
else a stop/target has to be invented without a live ATR read.

**Fix:** now imports and reuses `entry_engine.entry.FLAT_STOP_PCT`/
`FLAT_TARGET_PCT` for the fallback, and logs a `WARNING` (with the order
id/symbol) whenever this path is hit, since a missing decision link
should be rare and is worth investigating rather than silently patched
over.

## 5. Instrumented (not blindly changed): equity_sync.py's balance-field selection

**File:** `services/real-trade-service/execution/equity_sync.py`

`_pick_balance()` tries five differently-named/scoped Dhan balance fields
in priority order; I can't verify which ones actually populate on a real
account without live API responses, so I didn't reorder or guess new
field names. Instead: `_pick_balance()` now also returns which key
matched, and `sync_real_equity()` logs a `WARNING` the first time a key is
used and again any time the matched key **changes** between syncs (e.g.
Dhan silently stops populating `availableCash` and this starts falling
through to `sodLimit` instead). That's the actual failure mode worth
catching — a silent, unnoticed change in which balance definition is
sizing every trade — and it's now visible in the logs instead of hidden.

