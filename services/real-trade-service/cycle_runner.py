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

import logging

from sqlalchemy.orm import Session

import pipeline_status as pstat

logger = logging.getLogger("real-trade-cycle")


async def run_cycle_core(db: Session, mode: str, gate_armed: bool, trigger: str = "manual") -> dict:
    mode = mode.upper()

    # 2026-09-01 fix: a manual "Run Cycle" click bypasses Auto-Pilot's own
    # market-hours gate (only the automatic background loop was gated —
    # see execution/auto_pilot.py) and always ran the full scan, even
    # pre/post-market, silently. Left as a known follow-up in the previous
    # incident (dashboard 504 while "nothing running" — the actual cause
    # was a slow pre-market manual cycle, not something misbehaving).
    # Per admin: still process the cycle either way — just warn.
    warning = None
    if trigger == "manual":
        try:
            from tz_utils import is_market_open_ist, ist_now
            if not is_market_open_ist():
                when = ist_now().strftime("%H:%M IST")
                warning = f"Started outside market hours ({when}) — quotes may be stale/last-close, not live ticks"
                logger.warning("run_cycle_core: manual %s cycle started outside market hours (%s)", mode, when)
                try:
                    import notifier
                    await notifier.notify_async(
                        f"⚠️ Manual Run Cycle ({mode}) started outside market hours ({when}). "
                        f"Proceeding as requested — prices used may be stale/last-close."
                    )
                except Exception as _notify_err:
                    logger.debug("pre-market cycle warning notify failed (non-fatal): %s", _notify_err)
        except Exception as _gate_err:
            logger.debug("market-hours warning check failed (non-fatal, cycle still proceeds): %s", _gate_err)

    # Best-effort status tracking for the dashboard. Every call is guarded
    # so a bug here can NEVER affect the actual cycle (see pipeline_status.py
    # docstring) — worst case the dashboard shows stale/no progress.
    try:
        pstat.start_cycle(mode, trigger, warning=warning)
    except Exception:
        pass

    try:
        result = await _run_cycle_core(db, mode, gate_armed)
        if warning:
            result["pre_market_warning"] = warning
        return result
    except Exception as e:
        try:
            pstat.end_cycle(mode, {}, error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        raise


async def _run_cycle_core(db: Session, mode: str, gate_armed: bool) -> dict:
    if mode == "REAL":
        # §2 — TOTP refresh: attempt before the liveness check so a freshly-
        # expired token is renewed before we even call enforce_live_token.
        # Non-blocking: TOTP failure doesn't disarm — enforce_live_token will
        # catch any actual auth problem on the next line.
        #
        # BUG FIX (2026-09-01): this used to call refresh_if_totp_enabled()
        # UNCONDITIONALLY on every REAL cycle. With AUTO_PILOT_INTERVAL_SECONDS's
        # 180s default that's ~130 live calls/day to Dhan's generateAccessToken
        # endpoint even when the current token had hours of life left, plus a
        # "🔑 token refreshed" Telegram roughly every 3 minutes all day once
        # DHAN_TOTP_ENABLED was on. Gated with token_needs_refresh(db) — the
        # same check the standalone _totp_refresh_loop in execution/auto_pilot.py
        # uses — so this remains only a same-cycle safety net; the standalone
        # loop is the primary proactive path.
        try:
            from auth.dhan_credentials import refresh_if_totp_enabled, token_needs_refresh
            if token_needs_refresh(db):
                refresh_if_totp_enabled(db)
        except Exception as _totp_err:
            logger.debug("TOTP refresh attempt failed (non-fatal): %s", _totp_err)

        # §2 — Real-time token liveness check (original, unchanged).
        # A token Dhan already killed early (regenerated on Dhan Web, clock
        # drift) disarms the gate and Telegrams immediately, instead of
        # letting every order in the cycle fail silently one-by-one.
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
