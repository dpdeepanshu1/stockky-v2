# Short selling, Part 2 step 3: the EXIT side (23 Oct 2026). Position Stocks only. BUILT, no short can exist yet.

Longs behave exactly as before (legacy side NULL/""/LONG = long). Nothing opens a short: SHORT_MODE still cannot be live.

## What changed (position-stocks-service)
- NEW `orders/side.py` (stdlib only): `exit_transaction_type`, `entry_transaction_type`, `pnl`, `pnl_pct`, `target_hit_at`, `can_carry_overnight`. Unknown side raises (wrong-way close would double the position).
- `eod_squareoff.py`: flat close sends the exit side (BUY for a short); duplicate-order guard matches that side; shorts are never overnight-carry candidates, never get a CNC stop, and are refused as CNC; stagnation / no-follow-through uses the short's lowest price and side-aware gain.
- `reconcile.py`: all 6 SELL/BUY order-book and trade-history filters use the position's side; all 8 P&L formulas use `side.pnl`; dead-parent target/stop inference mirrored; leg re-arm levels mirrored (short target below, stop above, ratchet guard flipped).
- `overnight_stop.py`: partial-fill P&L side-aware (shorts never reach it).
- `stop_watchdog.py`: a short's stop is breached by price going UP through it.
- `charges_ledger.py`, `trade_report.py`: short swaps legs (sell leg = entry, buy leg = exit) so STT/stamp are right.
- `main.py`: /positions unrealized P&L side-aware.
- `breakeven.py`, `trailing.py`: SKIP shorts (fail-safe) until step 4 mirrors them.
- `close_all.py`, `exit_retry.py`: unchanged, already route through the side-aware close path.

## Tests
`tests/test_short_exit_side.py` (16, stdlib only) pass; `test_short_foundation.py` (8) pass; all edited files compile.

## NOT run
pytest and sqlalchemy cannot be installed in this sandbox (pypi blocked), so the position-stocks pytest suite was NOT run. On the VM run:
`python3 -m pytest tests -q` in position-stocks-service. Any existing test that pins old long-only text in these files would show there.
S0 (Dhan answer + 1-share test) is still open: nothing here is live.

## Next: step 4, mirrored levels + the order call (adaptive, rr_floor, sizing, ledger, SELL path in dhan_client, trailing/breakeven flips).
