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

import pipeline_status as pstat


async def run_cycle_core(db: Session, mode: str, gate_armed: bool, trigger: str = "manual") -> dict:
    mode = mode.upper()

    # Best-effort status tracking for the dashboard. Every call is guarded
    # so a bug here can NEVER affect the actual cycle (see pipeline_status.py
    # docstring) — worst case the dashboard shows stale/no progress.
    try:
        pstat.start_cycle(mode, trigger)
    except Exception:
        pass

    try:
        return await _run_cycle_core(db, mode, gate_armed)
    except Exception as e:
        try:
            pstat.end_cycle(mode, {}, error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        raise


async def _run_cycle_core(db: Session, mode: str, gate_armed: bool) -> dict:
    if mode == "REAL":
        # Real-time token check FIRST, before spending any Dhan calls or
        # doing any sizing this cycle: our local 24h countdown is correct
        # but can't see a token Dhan invalidated early (new token generated
        # elsewhere, clock drift, revocation). If Dhan itself rejects the
        # token, auto-disarm right here rather than proceeding to size and
        # attempt orders that will just fail auth one by one.
        from auth.dhan_credentials import enforce_live_token
        token_ok, token_err = enforce_live_token(db, mode)
        if not token_ok:
            early_result = {
                "mode": mode, "new_candidates": 0, "entry": {"evaluated": 0, "entered": 0, "waited": 0, "rejected": 0, "entry_details": []},
                "fills": 0, "expired_orders": 0, "exit": {}, "reconcile": None,
                "auto_disarmed": f"Dhan token rejected: {token_err}",
            }
            try:
                pstat.end_cycle(mode, early_result)
            except Exception:
                pass
            return early_result
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

    def _stage(name: str) -> None:
        try:
            pstat.set_stage(mode, name)
        except Exception:
            pass

    _stage("candidates")
    new_candidates = await refresh_candidates(db, mode)
    _stage("entry")
    entry_result = await entry_evaluate(db, mode, gate_armed)
    _stage("fills")
    fills = await check_pending_fills(db, mode)
    _stage("expire")
    expired = await expire_stale_orders(db, mode)
    _stage("exit")
    exit_result = await exit_evaluate(db, mode)

    reconcile_result = None
    if mode == "REAL":
        _stage("reconcile")
        from execution.reconcile import reconcile_real_orders
        reconcile_result = await reconcile_real_orders(db)
        fills = reconcile_result["entries_filled"]  # REAL "fills" only ever means broker-confirmed, never sent-only

    result = {
        "mode": mode,
        "new_candidates": new_candidates,
        "entry": entry_result,
        "fills": fills,
        "expired_orders": expired,
        "exit": exit_result,
        "reconcile": reconcile_result,
    }
    try:
        pstat.end_cycle(mode, result)
    except Exception:
        pass
    return result
