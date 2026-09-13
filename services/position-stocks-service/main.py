"""
main.py — position-stocks-service FastAPI entry point.

Lifecycle:
  startup:
    1. init_tables() — create scalp_* tables (+ auto-migrate any columns
       added to an existing table, see db.py::_ensure_columns)
    2. Start Angel One WS client (feed/ws_client.py)
    3. Warn loudly if RISK_PER_TRADE_PCT_CONFIRMED=false
  background loop (every 10s during market hours) — see _run_cycle():
    4. Reconcile exits — poll Dhan's super order book for TARGET_LEG/
       STOP_LOSS_LEG fills on OPEN positions, release capital (runs
       unconditionally — regardless of service_enabled/is_armed/
       auto_pilot_enabled — so exits already in flight are never orphaned)
    5. EOD squareoff check at 3:00 PM IST — ALSO unconditional (2026-09-12
       fix: previously this only ran if gate.is_armed was True, meaning a
       disarmed service with open positions would silently skip the hard
       flat-by-3pm sweep — the exact "no exceptions" case tracking doc §3.7
       exists to prevent.)
    6. service_enabled gate — if the whole module is toggled off
       (POST /service/disable), screening + entry stop here; reconciliation
       and EOD squareoff above still ran.
    7. is_armed gate — if not armed, screening still stops here too (no
       point scanning if entries can't fire), but EOD/reconciliation above
       already ran regardless.
    8. Run screening scan (1m/5m/15m/60m windows) — this ALWAYS runs once
       armed+enabled+market-open, regardless of auto_pilot_enabled, so
       GET /candidates stays live even with auto-pilot off (session 6).
    9. Quality gate (screening/quality_gate.py) checks the top
       config.QUALITY_GATE_TOP_N ranked candidates — best-effort, fast,
       fail-open — skipping any that fail and trying the next ranked one.
   10. auto_pilot_enabled gate — only the AUTOMATIC entry attempt is gated
       by this (added session 6); the background loop stops here if it's
       off, but a manual POST /cycle/run bypasses this specific check.
   11. Attempt entry on the first candidate that clears the quality gate.
  shutdown:
    stop WS client gracefully

API endpoints:
  POST /auth/login                   — admin login (SAME username/password as
                                          real-trade-service — see auth/admin_auth.py).
                                          Returns a Bearer token; every mutating
                                          route below requires it.
  POST /auth/logout                  — confirms token validity; stateless (frontend
                                          just drops the token locally)
  GET  /health                       — liveness probe
  GET  /status                       — full state dump
  POST /arm                          — arm the service (enable real orders)
  POST /disarm                       — disarm
  POST /service/enable                — re-enable the whole module (screening+entries)
  POST /service/disable               — pause the whole module (screening+entries);
                                          exit reconciliation + EOD squareoff still run
  POST /autopilot/enable              — resume automatic entries each cycle
  POST /autopilot/disable             — stop automatic entries; screening/candidates
                                          keep running so you can watch without acting
  POST /cycle/run                    — force one scan+entry cycle right now, bypassing
                                          the 10s timer and auto_pilot_enabled (still
                                          requires is_armed + service_enabled)
  GET  /positions                    — open + recent scalp positions (buy/sell price, P&L)
  GET  /trades/history                — full trade ledger with summary stats (win rate,
                                          total P&L), paginated
  GET  /candidates                   — latest screener output (no entry)
  GET  /ledger                       — capital ledger state
  POST /ledger/sync                  — force sync from Dhan
  POST /kill                         — manual kill switch (like /disarm + kill)
  GET  /ws-status                    — WebSocket feed status
  GET  /dhan/live-orders              — raw live Dhan super-order book for this
                                          service's tagged orders (broker-side truth)
  GET  /dhan/account                  — Dhan connection/token status + live funds,
                                          same shared account real-trade-service owns
  POST /reconcile                    — force an exit-reconciliation pass now
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("position-stocks-main")

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

import db as _db
from auth.admin_auth import (
    require_admin, verify_admin_password, issue_session_token, AdminAuthError,
)
from auth import dhan_credentials_ro
from capital import ledger, shared_order_budget
from execution import dhan_client
from feed import ws_client
from models import ScalpGateState, ScalpPosition
from orders import eod_squareoff, reconcile
from orders.entry import attempt_entry, log_quality_reject
from resilience import circuit_breaker
from screening import quality_gate
from screening.engine import scan
from tz_utils import ist_today_str, ist_time_at_or_after, is_market_open_ist, parse_hhmm


# ── Startup / shutdown ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB tables
    _db.init_tables()

    if not config.RISK_PER_TRADE_PCT_CONFIRMED:
        logger.warning(
            "⚠️  RISK_PER_TRADE_PCT_CONFIRMED is not set — using placeholder "
            "RISK_PER_TRADE_PCT=%.1f%%. Set RISK_PER_TRADE_PCT_CONFIRMED=true and "
            "RISK_PER_TRADE_PCT=<your chosen %> in env before going live.",
            config.RISK_PER_TRADE_PCT,
        )

    if not config.ADMIN_PASSWORD_HASH or not config.SESSION_SECRET:
        logger.warning(
            "⚠️  ADMIN_PASSWORD_HASH and/or SESSION_SECRET is not set — every "
            "arm/disarm/enable/kill/etc. request will 401 until the SAME "
            "ADMIN_PASSWORD_HASH (or _B64) / SESSION_SECRET already used by "
            "real-trade-service's .env is present for this service too."
        )

    # Start the Angel One WebSocket feed
    await ws_client.start()
    logger.info("position-stocks-service: ready on port %d", config.PORT)

    # Start background trading loop
    _bg_task = asyncio.create_task(_trading_loop(), name="position-stocks-trading-loop")

    yield

    # Graceful shutdown
    _bg_task.cancel()
    try:
        await _bg_task
    except asyncio.CancelledError:
        pass
    await ws_client.stop()
    logger.info("position-stocks-service: shutdown complete")


app = FastAPI(title="position-stocks-service", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_EOD_SQUAREOFF_TIME = parse_hhmm(config.EOD_SQUAREOFF_TIME_IST, 15, 0)
_SCAN_INTERVAL_S = 10.0   # run screener every 10 seconds
_cycle_lock = asyncio.Lock()  # prevents the background loop and a manual
                               # POST /cycle/run from overlapping


async def _run_cycle(db: Session, trigger: str) -> dict:
    """One full cycle: reconcile → EOD check → screen → quality-gate the
    top candidates → attempt entry on the first that passes. Shared by the
    background loop (trigger="AUTO") and POST /cycle/run (trigger="MANUAL")
    so the two can never drift apart in behavior. Returns a small summary
    dict for the manual endpoint's response; the background loop ignores it."""
    summary = {"reconciled": 0, "eod_fired": False, "candidates_seen": 0,
               "entered_symbol": None, "skipped_reason": None}

    # Reconcile exits first, and unconditionally — a position's TARGET_LEG/
    # STOP_LOSS_LEG can fill on Dhan's side whether or not this service is
    # currently armed/enabled/auto-piloting for NEW entries, and an orphaned
    # OPEN row blocks its symbol from ever being re-scanned plus keeps its
    # capital stuck reserved.
    try:
        n_closed = reconcile.run_exit_reconciliation(db)
        summary["reconciled"] = n_closed
        if n_closed:
            logger.info("position-stocks: reconciled %d exit(s)", n_closed)
    except Exception as e:
        logger.error("position-stocks: reconciliation error: %s", e, exc_info=True)

    gate = db.query(ScalpGateState).filter_by(mode="REAL").first()

    # EOD squareoff gate — unconditional: runs regardless of
    # service_enabled/is_armed/auto_pilot_enabled, same reasoning as exit
    # reconciliation above (tracking doc §3.7: "no exceptions").
    today = ist_today_str()
    eod_fired = gate and gate.eod_squareoff_fired_date == today
    if ist_time_at_or_after(_EOD_SQUAREOFF_TIME) and not eod_fired:
        logger.info("position-stocks: EOD squareoff time reached — running sweep")
        eod_squareoff.run_eod_squareoff(db)
        summary["eod_fired"] = True
        return summary

    if gate is None or not gate.service_enabled:
        summary["skipped_reason"] = "SERVICE_DISABLED"
        return summary

    if not gate.is_armed:
        summary["skipped_reason"] = "NOT_ARMED"
        return summary

    # Don't enter new positions after EOD squareoff time
    if ist_time_at_or_after(_EOD_SQUAREOFF_TIME):
        summary["skipped_reason"] = "PAST_EOD_TIME"
        return summary

    # Get open symbols to exclude from candidates
    open_syms = {
        p.symbol
        for p in db.query(ScalpPosition).filter_by(status="OPEN").all()
    }

    candidates = scan(open_symbols=open_syms)
    summary["candidates_seen"] = len(candidates)
    if not candidates:
        return summary

    # Screening runs regardless of auto_pilot_enabled (so /candidates stays
    # live) — only the automatic ENTRY is gated by it. A manual /cycle/run
    # deliberately bypasses this one check (mirroring real-trade-service's
    # manual cycle trigger working regardless of auto-pilot state).
    if trigger == "AUTO" and not gate.auto_pilot_enabled:
        summary["skipped_reason"] = "AUTO_PILOT_OFF"
        return summary

    # Quality-gate the top few ranked candidates (fast, best-effort, fail-
    # open — see screening/quality_gate.py) and enter the first that passes.
    top_n = candidates[: max(1, config.QUALITY_GATE_TOP_N)]
    for candidate in top_n:
        quality = await quality_gate.check(candidate.symbol)
        ok, reason = quality.passes()
        if not ok:
            logger.info("position-stocks: quality gate skipped %s — %s", candidate.symbol, reason)
            log_quality_reject(db, candidate, quality, reason)
            continue

        result = attempt_entry(db, candidate, quality=quality)
        if result:
            circuit_breaker.record_success()
            summary["entered_symbol"] = candidate.symbol
        # Note: attempt_entry logs SKIPPED reasons internally either way
        break  # only ever try one candidate per cycle, same as before

    gate.last_cycle_run_at = datetime.now(timezone.utc)
    gate.last_cycle_run_trigger = trigger
    db.commit()
    return summary


async def _trading_loop() -> None:
    """Background coroutine: runs _run_cycle(trigger="AUTO") every
    _SCAN_INTERVAL_S during market hours."""
    while True:
        try:
            await asyncio.sleep(_SCAN_INTERVAL_S)
            if circuit_breaker.is_open():
                continue
            if not is_market_open_ist():
                continue

            factory = _db.get_session_factory()
            if factory is None:
                continue

            async with _cycle_lock:
                with factory() as db:
                    await _run_cycle(db, trigger="AUTO")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("position-stocks trading loop error: %s", e, exc_info=True)
            circuit_breaker.record_failure()


# ── FastAPI dependency ───────────────────────────────────────────────────────
def get_db():
    yield from _db.get_db()


def _get_gate(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


class LoginRequest(BaseModel):
    username: str
    password: str


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "position-stocks-service"}


@app.post("/auth/login")
def login(body: LoginRequest):
    """Same admin username/password as real-trade-service — verified against
    the SAME ADMIN_PASSWORD_HASH / ADMIN_USERNAME env vars. Generic 401 on
    any failure (wrong username, wrong password, or auth not configured on
    this deploy) so a caller can never distinguish which — avoids username
    enumeration, matches real-trade-service's /auth/login behavior."""
    try:
        ok = verify_admin_password(body.username, body.password)
    except AdminAuthError as e:
        logger.error("position-stocks: login attempted but auth not configured: %s", e)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, expires_at = issue_session_token(body.username)
    logger.info("position-stocks-service: admin login (%s)", body.username)
    return {"token": token, "expires_at": expires_at}


@app.post("/auth/logout")
def logout(admin: str = Depends(require_admin)):
    """Stateless JWT — there is no server-side session row to clear here
    (position-stocks-service doesn't persist an admin_authenticated gate
    flag the way real-trade-service does), so this just confirms the token
    was valid; the frontend drops it locally. The token remains technically
    valid until its short SESSION_IDLE_TIMEOUT_MINUTES expiry either way."""
    logger.info("position-stocks-service: admin logout (%s)", admin)
    return {"status": "logged_out"}


@app.get("/status")
def status(db: Session = Depends(get_db)):
    gate = _get_gate(db)
    return {
        "armed": gate.is_armed,
        "armed_at": gate.armed_at,
        "service_enabled": gate.service_enabled,
        "auto_pilot_enabled": gate.auto_pilot_enabled,
        "last_cycle_run_at": gate.last_cycle_run_at,
        "last_cycle_run_trigger": gate.last_cycle_run_trigger,
        "first_live_order_done": gate.first_live_order_done,
        "orders_placed_today": gate.orders_placed_today,
        "daily_loss_kill_switch": gate.daily_loss_kill_switch_tripped,
        "eod_squareoff_fired_date": gate.eod_squareoff_fired_date,
        "shared_order_budget": shared_order_budget.status(db),
        "circuit_breaker": circuit_breaker.status(),
        "ws": ws_client.ws_status(),
        "market_open": is_market_open_ist(),
        "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
        "risk_confirmed": config.RISK_PER_TRADE_PCT_CONFIRMED,
        # Read-only risk-config snapshot for the dashboard's Risk Configuration
        # card (mirrors Real Auto Trade's, but these knobs are env/config-driven
        # here, not a DB row an admin edits in-app — see config.py).
        "max_daily_loss_pct_of_pool": config.MAX_DAILY_LOSS_PCT_OF_POOL,
        "max_concurrent_scalp_positions": config.MAX_CONCURRENT_SCALP_POSITIONS,
        "scalp_pool_capital_share_pct": config.SCALP_POOL_CAPITAL_SHARE_PCT,
    }


@app.post("/arm")
def arm(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    gate = _get_gate(db)
    if gate.is_armed:
        return {"status": "already_armed"}
    gate.is_armed = True
    gate.armed_at = datetime.now(timezone.utc)
    db.commit()
    logger.warning("position-stocks-service: ARMED — real orders enabled")
    return {"status": "armed"}


@app.post("/disarm")
def disarm(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    gate = _get_gate(db)
    gate.is_armed = False
    db.commit()
    logger.warning("position-stocks-service: DISARMED")
    return {"status": "disarmed"}


@app.post("/service/enable")
def service_enable(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Re-enable the whole module (screening + entries). Independent of
    is_armed — this only controls whether the module runs at all, not
    whether it's allowed to place real orders once running."""
    gate = _get_gate(db)
    gate.service_enabled = True
    db.commit()
    logger.warning("position-stocks-service: module ENABLED (service_enabled=true)")
    return {"status": "enabled"}


@app.post("/service/disable")
def service_disable(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Pause the whole module (screening + entries stop). Exit
    reconciliation and the EOD square-off sweep keep running regardless —
    open real-money positions are never left unmanaged just because the
    module is toggled off (tracking doc §3.7: "no exceptions")."""
    gate = _get_gate(db)
    gate.service_enabled = False
    db.commit()
    logger.warning("position-stocks-service: module DISABLED (service_enabled=false) — "
                    "reconciliation + EOD squareoff still active")
    return {"status": "disabled"}


@app.post("/autopilot/enable")
def autopilot_enable(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Resume automatic entries each cycle. Screening keeps running either
    way once armed+service_enabled+market-open — this only controls
    whether the loop acts on what it finds."""
    gate = _get_gate(db)
    gate.auto_pilot_enabled = True
    db.commit()
    logger.warning("position-stocks-service: AUTO-PILOT ENABLED")
    return {"status": "auto_pilot_enabled"}


@app.post("/autopilot/disable")
def autopilot_disable(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Stop automatic entries. Screening/candidates keep updating live so
    you can watch what the engine would do without it acting — use
    POST /cycle/run for a one-off manual push while auto-pilot is off."""
    gate = _get_gate(db)
    gate.auto_pilot_enabled = False
    db.commit()
    logger.warning("position-stocks-service: AUTO-PILOT DISABLED — screening still live")
    return {"status": "auto_pilot_disabled"}


@app.post("/cycle/run")
async def cycle_run(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Force one scan+entry cycle right now, bypassing the 10s timer and
    auto_pilot_enabled (still requires is_armed + service_enabled — a
    paused/disarmed service won't fire a manual cycle either, same
    reasoning as the background loop). Takes the same lock the background
    loop uses so the two can never run concurrently and double-enter."""
    gate = _get_gate(db)
    if not gate.service_enabled:
        raise HTTPException(status_code=400, detail="Module is disabled (service_enabled=false) — enable it first.")
    if not gate.is_armed:
        raise HTTPException(status_code=400, detail="Service is not armed — arm it first.")

    if _cycle_lock.locked():
        raise HTTPException(status_code=409, detail="A cycle is already running — try again shortly.")

    async with _cycle_lock:
        summary = await _run_cycle(db, trigger="MANUAL")
    return {"status": "ok", **summary}


@app.post("/kill")
def kill(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Manual kill switch: disarm + trip daily loss kill switch."""
    gate = _get_gate(db)
    gate.is_armed = False
    gate.daily_loss_kill_switch_tripped = True
    gate.daily_loss_kill_switch_tripped_date = ist_today_str()
    db.commit()
    logger.warning("position-stocks-service: MANUAL KILL SWITCH ACTIVATED")
    return {"status": "killed"}


@app.get("/positions")
def positions(db: Session = Depends(get_db)):
    """Open + recent scalp positions. Includes exit_price (added session 6)
    so the dashboard can show buy price vs. sell price side by side, not
    just the realized P&L that was derived from them."""
    rows = db.query(ScalpPosition).order_by(ScalpPosition.opened_at.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "status": r.status,
            "window_source": r.window_source,
            "entry_price": r.entry_price,
            "exit_price": r.exit_price,
            "quantity": r.quantity,
            "target_price": r.target_price,
            "stop_price": r.stop_price,
            "adaptive_target_pct": r.adaptive_target_pct,
            "adaptive_stop_pct": r.adaptive_stop_pct,
            "realized_pnl": r.realized_pnl,
            "realized_pnl_pct": r.realized_pnl_pct,
            "opened_at": r.opened_at,
            "closed_at": r.closed_at,
            "is_first_live_order": r.is_first_live_order,
            "dhan_super_order_id": r.dhan_super_order_id,
        }
        for r in rows
    ]


@app.get("/trades/history")
def trades_history(
    db: Session = Depends(get_db),
    limit: int = 200,
    status_filter: Optional[str] = None,
):
    """Full trade ledger — every closed (and currently open) scalp position,
    with buy price, sell price, and P&L per trade, plus summary stats
    (win rate, total P&L, best/worst trade) for a proper track-record view.
    `status_filter` optionally narrows to one status (e.g. "TARGET_HIT")."""
    q = db.query(ScalpPosition).order_by(ScalpPosition.opened_at.desc())
    if status_filter:
        q = q.filter_by(status=status_filter.upper())
    rows = q.limit(max(1, min(limit, 1000))).all()

    closed = [r for r in rows if r.status != "OPEN" and r.realized_pnl is not None]
    wins = [r for r in closed if r.realized_pnl > 0]
    losses = [r for r in closed if r.realized_pnl <= 0]
    total_pnl = sum(r.realized_pnl for r in closed) if closed else 0.0
    best = max(closed, key=lambda r: r.realized_pnl) if closed else None
    worst = min(closed, key=lambda r: r.realized_pnl) if closed else None

    return {
        "summary": {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(closed), 1) if closed else None,
            "total_pnl": round(total_pnl, 2),
            "best_trade": {"symbol": best.symbol, "pnl": best.realized_pnl} if best else None,
            "worst_trade": {"symbol": worst.symbol, "pnl": worst.realized_pnl} if worst else None,
        },
        "trades": [
            {
                "id": r.id,
                "symbol": r.symbol,
                "status": r.status,
                "window_source": r.window_source,
                "entry_price": r.entry_price,
                "exit_price": r.exit_price,
                "quantity": r.quantity,
                "realized_pnl": r.realized_pnl,
                "realized_pnl_pct": r.realized_pnl_pct,
                "opened_at": r.opened_at,
                "closed_at": r.closed_at,
                "dhan_super_order_id": r.dhan_super_order_id,
            }
            for r in rows
        ],
    }


@app.get("/candidates")
def candidates():
    """Run a live scan and return the top-20 candidates without entering."""
    results = scan()
    return [
        {
            "symbol": c.symbol,
            "window": c.window_label,
            "pct_change": c.pct_change,
            "current_ltp": c.current_ltp,
            "composite_score": c.composite_score,
            "tick_activity": c.tick_activity,
        }
        for c in results[:20]
    ]


@app.get("/ledger")
def get_ledger(db: Session = Depends(get_db)):
    return ledger.get_state(db)


@app.post("/ledger/sync")
def sync_ledger(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    total = ledger.sync_from_broker(db)
    return {"status": "synced", "total_allocated_capital": total}


@app.get("/ws-status")
def ws_status_route():
    return ws_client.ws_status()


@app.get("/dhan/account")
def dhan_account(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Dhan Account card for this dashboard — same shape as real-trade-
    service's GET /dhan/account (see auth/dhan_credentials_ro.connection_status
    docstring for why). Connection/token state is always available (DB-only,
    read from the row real-trade-service owns); funds is a live Dhan call
    that proves the shared token still actually works right now, not just
    that it hasn't expired on paper. A failed funds call does not hide the
    connection state — the dashboard can show "connected but funds call
    failed: <reason>" instead of going blank."""
    status = dhan_credentials_ro.connection_status(db)
    funds = None
    funds_error = None
    if status["connected"]:
        try:
            funds = dhan_client.get_funds(db)
        except Exception as e:
            funds_error = str(e)[:300]
    return {**status, "funds": funds, "funds_error": funds_error}


@app.get("/dhan/live-orders")
def dhan_live_orders(db: Session = Depends(get_db)):
    """Raw live Dhan super-order book — broker-side truth, not this
    service's own DB view. Mirrors real-trade-service's "Dhan Live" tab:
    lets you see exactly what Dhan itself thinks is happening (order/leg
    status, prices) independent of whatever `/positions` currently shows,
    which is useful for spotting a reconciliation lag or a leg that filled
    on Dhan's side before this service's next 10s poll picks it up.
    Read-only — no arm check needed (get_super_order_list itself doesn't
    require armed). Filters to orders tagged "SCALP" when Dhan returns a
    tag field, so this never shows real-trade-service's own orders even
    though both use the same Dhan account."""
    try:
        orders = dhan_client.get_super_order_list(db)
    except Exception as e:
        logger.error("position-stocks: /dhan/live-orders fetch failed: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Dhan fetch failed: {e}")

    scalp_orders = [o for o in orders if not isinstance(o, dict) or o.get("tag", "SCALP") == "SCALP"]
    return {"count": len(scalp_orders), "orders": scalp_orders}


@app.post("/reconcile")
def reconcile_route(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Force an exit-reconciliation pass right now (same check the
    background loop runs every 10s) — useful right after a manual Dhan-side
    action, or to confirm a fill immediately instead of waiting for the
    next tick."""
    closed = reconcile.run_exit_reconciliation(db)
    return {"status": "ok", "positions_closed": closed}
