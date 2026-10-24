# Short selling step 4: mirrored levels, sizing, capital and the SELL order call (Position Stocks only)

Nothing can open a short yet: `orders/entry.py` is unchanged and still places BUY entries only, and `SHORT_MODE` still cannot be set to live. Every long path is byte-for-byte the same in behaviour (checked on 836 random adaptive-level and sizing cases, old zip vs new: identical).

## What changed
- `execution/dhan_client.py`: new pure `clamp_super_levels(side, ref, target, stop)` (BUY: target above / stop below ref; SELL mirrored, one-tick clamps; unknown side raises; a SELL target that would reach zero is refused). `place_super_order` uses it. A LIMIT entry with a leg on the wrong side of price raises with symbol context. `convert_position` refuses SHORT. `match_recent_buy_order` / `find_recent_buy_order` take a `transaction_type` (default BUY) so a SELL entry can be matched after a timeout.
- `orders/adaptive.py`: `AdaptiveLevels.side`, `normalize_side`, mirrored room (a short has little room near the day LOW), mirrored prices in every path (legacy, bar-ATR, target cap). Unknown side raises.
- `orders/rr_floor.py`: `geometry_reason` rejects levels on the wrong side of price for the direction.
- `orders/sizing.py`: `compute_quantity(..., side)`; a SHORT is floored, never rounded up, never forced to 1 share past the budget (None = skip).
- `capital/ledger.py`: `reserve_capital(..., side)`; a SHORT is refused unless `SHORT_MODE` is live (read at call time) and risks `SHORT_RISK_MULT` of a long. New switch `SHORT_RISK_MULT` (0.1 to 1.0, ships 0.5) in `config.py`, `control/catalog.py` and the calibrator's PROTECTED list.
- `orders/trailing.py`, `orders/breakeven.py`: the step-3 "skip shorts" guards are replaced by the real mirror (low-water mark, stop trails above it, breakeven when the price has fallen to the trigger, never an exit-now clamp).
- `docs/ENV_REFERENCE.md` regenerated.

## Tests
New `tests/test_short_step4_levels_orders.py`. Four tests pinned step-3 wording and were updated: `test_short_exit_side` (trailing/breakeven now side-aware), `test_short_foundation` (the ledger is the one allowed reader of SHORT_MODE), and the step-4 room and sizing tests. Three pre-existing failures noted last session are fixed in `test_e37_catalog_coverage`, `test_ledger_coverage` and `test_live_anchor_20261022`.

## Not done (next)
Step 5: `orders/entry.py` `attempt_entry(side)` with the mirrored gates, shadow row, scanner `scan_shorts()`. S0 (Dhan written answer plus the 1-share test) is still open.
