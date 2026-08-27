"""
cycle_runner.py — the single implementation of "run one evaluation cycle
for a mode": refresh candidates -> evaluate entries -> check fills ->
expire stale orders -> evaluate exits -> (REAL only) reconcile with Dhan.

Extracted out of main.py's POST /cycle/run/{mode} route so the exact same
logic can be called from two places without duplicating it:
  1. The route itself (manual "Run Cycle" button / API call).
  2. execution/auto_pilot.py's background timer (the "even when the
     website isn't open" path) — see that module's docstring.

Callers are responsible for checking the gate (armed, and for auto-pilot,
auto_pilot_enabled) BEFORE calling this — this module trusts gate_armed
as given and does not re-check it against the DB itself.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


async def run_cycle_core(db: Session, mode: str, gate_armed: bool) -> dict:
    mode = mode.upper()

    if mode == "REAL":
        # Refresh cash_available/current_equity from Dhan's live balance
        # once at the top of the cycle (entry_engine also does this per
        # candidate, but doing it here too means /status and the exit
        # cycle's own equity display are just as fresh, at negligible
        # extra cost — get_funds is a single cheap read-only Dhan call).
        from execution.equity_sync import sync_real_equity
        sync_real_equity(db)

    from candidate_engine.candidates import refresh_candidates
    from entry_engine.entry import evaluate_mode as entry_evaluate, check_pending_fills, expire_stale_orders
    from exit_engine.exit import evaluate_mode as exit_evaluate

    new_candidates = await refresh_candidates(db, mode)
    entry_result = await entry_evaluate(db, mode, gate_armed)
    fills = await check_pending_fills(db, mode)
    expired = await expire_stale_orders(db, mode)
    exit_result = await exit_evaluate(db, mode)

    reconcile_result = None
    if mode == "REAL":
        from execution.reconcile import reconcile_real_orders
        reconcile_result = await reconcile_real_orders(db)
        fills = reconcile_result["entries_filled"]  # REAL "fills" only ever means broker-confirmed, never sent-only

    return {
        "mode": mode,
        "new_candidates": new_candidates,
        "entry": entry_result,
        "fills": fills,
        "expired_orders": expired,
        "exit": exit_result,
        "reconcile": reconcile_result,
    }
