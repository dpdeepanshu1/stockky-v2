"""
main.py — position-stocks-service FastAPI entry point.

Lifecycle:
  startup:
    1. init_tables() — create scalp_* tables
    2. Start Angel One WS client (feed/ws_client.py)
    3. Warn loudly if RISK_PER_TRADE_PCT_CONFIRMED=false
  background loop (every 10s during market hours):
    4. Reconcile exits — poll Dhan's super order book for TARGET_LEG/
       STOP_LOSS_LEG fills on OPEN positions, release capital (runs
       unconditionally — regardless of service_enabled/is_armed — so
       exits already in flight are never orphaned)
    5. EOD squareoff check at 3:00 PM IST — ALSO unconditional (2026-09-12
       fix: previously this only ran if gate.is_armed was True, meaning a
       disarmed service with open positions would silently skip the hard
       flat-by-3pm sweep — the exact "no exceptions" case tracking doc §3.7
       exists to prevent. Moved ahead of both the service_enabled and
       is_armed checks below.)
    6. service_enabled gate — if the whole module is toggled off
       (POST /service/disable), screening + entry stop here; reconciliation
       and EOD squareoff above still ran.
    7. is_armed gate — if not armed, screening still stops here too (no
       point scanning if entries can't fire), but EOD/reconciliation above
       already ran regardless.
    8. Run screening scan (now across 1m/5m/15m/60m windows)
    9. Attempt entry on top candidate(s)
  shutdown:
    stop WS client gracefully

API endpoints:
  GET  /health                       — liveness probe
  GET  /status                       — full state dump
  POST /arm                          — arm the service (enable real orders)
  POST /disarm                       — disarm
  POST /service/enable                — re-enable the whole module (screening+entries)
  POST /service/disable               — pause the whole module (screening+entries);
                                          exit reconciliation + EOD squareoff still run
  GET  /positions                    — open scalp positions
  GET  /candidates                   — latest screener output (no entry)
  GET  /ledger                       — capital ledger state
  POST /ledger/sync                  — force sync from Dhan
  POST /kill                         — manual kill switch (like /disarm + kill)
  GET  /ws-status                    — WebSocket feed status
  POST /reconcile                    — force an exit-reconciliation pass now
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("position-stocks-main")

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import db as _db
from capital import ledger
from feed import ws_client
from models import ScalpGateState, ScalpPosition
from orders import eod_squareoff, reconcile
from orders.entry import attempt_entry
from resilience import circuit_breaker
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


async def _trading_loop() -> None:
    """Background coroutine: scan → entry → EOD check, every _SCAN_INTERVAL_S."""
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

            with factory() as db:
                # Reconcile exits first, and unconditionally — a position's
                # TARGET_LEG/STOP_LOSS_LEG can fill on Dhan's side whether
                # or not this service is currently armed for NEW entries,
                # and an orphaned OPEN row blocks its symbol from ever
                # being re-scanned plus keeps its capital stuck reserved.
                try:
                    n_closed = reconcile.run_exit_reconciliation(db)
                    if n_closed:
                        logger.info("position-stocks: reconciled %d exit(s)", n_closed)
                except Exception as e:
                    logger.error("position-stocks: reconciliation error: %s", e, exc_info=True)

                gate = db.query(ScalpGateState).filter_by(mode="REAL").first()

                # EOD squareoff gate — unconditional (2026-09-12 fix): this used
                # to sit after the is_armed check below, so a disarmed service
                # with open positions silently skipped the hard 3pm flat sweep.
                # Runs regardless of service_enabled/is_armed, same reasoning as
                # exit reconciliation above — see main.py's module docstring.
                today = ist_today_str()
                eod_fired = gate and gate.eod_squareoff_fired_date == today
                if ist_time_at_or_after(_EOD_SQUAREOFF_TIME) and not eod_fired:
                    logger.info("position-stocks: EOD squareoff time reached — running sweep")
                    eod_squareoff.run_eod_squareoff(db)
                    continue

                if gate is None or not gate.service_enabled:
                    continue

                if not gate.is_armed:
                    continue

                # Don't enter new positions after EOD squareoff time
                if ist_time_at_or_after(_EOD_SQUAREOFF_TIME):
                    continue

                # Get open symbols to exclude from candidates
                open_syms = {
                    p.symbol
                    for p in db.query(ScalpPosition).filter_by(status="OPEN").all()
                }

                candidates = scan(open_symbols=open_syms)
                if not candidates:
                    continue

                # Try to enter the top candidate (highest composite score)
                top = candidates[0]
                result = attempt_entry(db, top)
                if result:
                    circuit_breaker.record_success()
                # Note: attempt_entry logs SKIPPED reasons internally

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


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "position-stocks-service"}


@app.get("/status")
def status(db: Session = Depends(get_db)):
    gate = _get_gate(db)
    return {
        "armed": gate.is_armed,
        "armed_at": gate.armed_at,
        "service_enabled": gate.service_enabled,
        "first_live_order_done": gate.first_live_order_done,
        "orders_placed_today": gate.orders_placed_today,
        "daily_loss_kill_switch": gate.daily_loss_kill_switch_tripped,
        "eod_squareoff_fired_date": gate.eod_squareoff_fired_date,
        "circuit_breaker": circuit_breaker.status(),
        "ws": ws_client.ws_status(),
        "market_open": is_market_open_ist(),
        "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
        "risk_confirmed": config.RISK_PER_TRADE_PCT_CONFIRMED,
    }


@app.post("/arm")
def arm(db: Session = Depends(get_db)):
    gate = _get_gate(db)
    if gate.is_armed:
        return {"status": "already_armed"}
    gate.is_armed = True
    gate.armed_at = datetime.now(timezone.utc)
    db.commit()
    logger.warning("position-stocks-service: ARMED — real orders enabled")
    return {"status": "armed"}


@app.post("/disarm")
def disarm(db: Session = Depends(get_db)):
    gate = _get_gate(db)
    gate.is_armed = False
    db.commit()
    logger.warning("position-stocks-service: DISARMED")
    return {"status": "disarmed"}


@app.post("/service/enable")
def service_enable(db: Session = Depends(get_db)):
    """Re-enable the whole module (screening + entries). Independent of
    is_armed — this only controls whether the module runs at all, not
    whether it's allowed to place real orders once running."""
    gate = _get_gate(db)
    gate.service_enabled = True
    db.commit()
    logger.warning("position-stocks-service: module ENABLED (service_enabled=true)")
    return {"status": "enabled"}


@app.post("/service/disable")
def service_disable(db: Session = Depends(get_db)):
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


@app.post("/kill")
def kill(db: Session = Depends(get_db)):
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
    rows = db.query(ScalpPosition).order_by(ScalpPosition.opened_at.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "symbol": r.symbol,
            "status": r.status,
            "window_source": r.window_source,
            "entry_price": r.entry_price,
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
def sync_ledger(db: Session = Depends(get_db)):
    total = ledger.sync_from_broker(db)
    return {"status": "synced", "total_allocated_capital": total}


@app.get("/ws-status")
def ws_status_route():
    return ws_client.ws_status()


@app.post("/reconcile")
def reconcile_route(db: Session = Depends(get_db)):
    """Force an exit-reconciliation pass right now (same check the
    background loop runs every 10s) — useful right after a manual Dhan-side
    action, or to confirm a fill immediately instead of waiting for the
    next tick."""
    closed = reconcile.run_exit_reconciliation(db)
    return {"status": "ok", "positions_closed": closed}
