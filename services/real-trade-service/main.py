"""
real-trade-service — admin auth, DB schema, gate/arming state machine,
risk engine, Dhan credential storage, live Dhan account status, manual
trade ticket, automatic entry/exit cycle, and Auto-Pilot (a background
loop that runs that cycle on a timer so armed trading keeps working with
the dashboard closed — see execution/auto_pilot.py). Off by default per
mode; arming alone never causes it to run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config
import models
from auth.admin_auth import (
    require_admin, require_admin_if_real, verify_admin_password, issue_session_token, AdminAuthError,
)
from auth import dhan_credentials
from audit.logger import log_action
from db import get_db, init_schema
from tz_utils import as_aware
from portfolio.portfolio import open_positions as _pf_open_positions, close_position as _pf_close_position
from execution import dhan_client
from risk_engine.engine import AccountState, OrderIntent, evaluate as risk_evaluate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("real-trade-service")

app = FastAPI(title="Stockky Real Automatic Trade", version="0.1.0-phase1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tightened at the gateway/proxy layer, same as other Stockky services
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    # Auto-Pilot (2026-08-27): a single asyncio background task IS started
    # here — execution/auto_pilot.py — but it is inert by default. It only
    # ever acts on a mode that is BOTH armed AND has auto_pilot_enabled=True
    # (off by default for every mode, see models.TradeGateState), so simply
    # booting this service does not cause anything to trade on its own.
    # /cycle/run/{mode} remains available as a manual trigger regardless of
    # whether auto-pilot is on for that mode.
    errors = config.startup_config_errors()
    if errors:
        # Fail loud, not silent: booting "successfully" without these would
        # mean a /login route that can never succeed, or credentials that
        # can't be safely stored — worse than not booting at all.
        for e in errors:
            logger.error("CONFIG ERROR: %s", e)
        raise RuntimeError(f"real-trade-service refusing to start: {'; '.join(errors)}")
    init_schema()
    _seed_defaults()
    from execution import auto_pilot
    auto_pilot.start()
    logger.info("real-trade-service ready (Phase 1 — demo skeleton, no live order path wired)")


def _seed_defaults() -> None:
    """One-time seed of DEMO/REAL account, risk-config, and gate-state rows
    if they don't exist yet. Never overwrites an existing row (admin-edited
    risk config must survive restarts)."""
    from db import get_session_factory
    Session = get_session_factory()
    db = Session()
    try:
        for mode in ("DEMO", "REAL"):
            if not db.query(models.TradeGateState).filter_by(mode=mode).first():
                gate_kwargs = {"mode": mode}
                if mode == "DEMO":
                    # DEMO is open by design (2026-08-25) — pre-satisfy the
                    # admin/risk-config gates permanently so /arm/DEMO needs
                    # nothing beyond the call itself. REAL still starts with
                    # every gate False, walked in order as before.
                    gate_kwargs.update(
                        admin_authenticated=True,
                        admin_authenticated_at=datetime.now(timezone.utc),
                        admin_session_expires_at=None,  # never expires for DEMO — see _check_and_expire_gates
                        risk_config_confirmed=True,
                        risk_config_confirmed_at=datetime.now(timezone.utc),
                    )
                db.add(models.TradeGateState(**gate_kwargs))
            if not db.query(models.TradeRiskConfig).filter_by(mode=mode).first():
                db.add(models.TradeRiskConfig(
                    mode=mode,
                    risk_per_trade_pct=config.DEFAULT_RISK_PER_TRADE_PCT,
                    max_daily_loss_pct=config.DEFAULT_MAX_DAILY_LOSS_PCT,
                    max_concurrent_positions=config.DEFAULT_MAX_CONCURRENT_POSITIONS,
                    max_portfolio_risk_pct=config.DEFAULT_MAX_PORTFOLIO_RISK_PCT,
                    stale_data_seconds=config.DEFAULT_STALE_DATA_SECONDS,
                    max_tick_volatility_mult=config.DEFAULT_MAX_TICK_VOLATILITY_MULT,
                ))
            if not db.query(models.TradeAccount).filter_by(mode=mode).first():
                capital = config.DEFAULT_DEMO_CAPITAL if mode == "DEMO" else 0.0
                db.add(models.TradeAccount(
                    mode=mode, starting_capital=capital, current_equity=capital, cash_available=capital,
                ))
        db.commit()
    finally:
        db.close()


# ── Request/response models ─────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class ConnectDhanRequest(BaseModel):
    client_id: str
    access_token: str


class RiskConfigUpdate(BaseModel):
    mode: str
    risk_per_trade_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_concurrent_positions: Optional[int] = None
    max_portfolio_risk_pct: Optional[float] = None
    stale_data_seconds: Optional[int] = None
    max_tick_volatility_mult: Optional[float] = None
    allow_pyramiding: Optional[bool] = None


# ── Gate helpers ─────────────────────────────────────────────────────────────
def _gate(db: Session, mode: str) -> models.TradeGateState:
    row = db.query(models.TradeGateState).filter_by(mode=mode).first()
    if row is None:
        raise HTTPException(status_code=500, detail=f"Gate state missing for mode={mode} (schema not seeded).")
    return row


def _disarm(db: Session, mode: str, reason: str, actor: str = "system") -> None:
    gate = _gate(db, mode)
    was_armed = gate.armed
    gate.armed = False
    gate.disarmed_reason = reason
    gate.updated_at = datetime.now(timezone.utc)
    db.commit()
    if was_armed:
        log_action(db, actor=actor, action="DISARMED", detail=reason, mode=mode)


def _check_and_expire_gates(db: Session, mode: str) -> models.TradeGateState:
    """Called at the top of every gate-status read — expires the admin
    session gate and the Dhan token gate independently, per the plan's
    'each gate can expire on its own' design. Never re-arms anything; only
    ever tightens state."""
    gate = _gate(db, mode)
    now = datetime.now(timezone.utc)

    if gate.admin_authenticated and gate.admin_session_expires_at and now > as_aware(gate.admin_session_expires_at):
        gate.admin_authenticated = False
        if gate.armed:
            _disarm(db, mode, "Admin session expired")
        db.commit()

    if mode == "REAL" and gate.dhan_connected:
        cred = db.query(models.TradeCredential).first()
        if cred is None or not dhan_credentials.is_token_valid(cred):
            gate.dhan_connected = False
            if gate.armed:
                _disarm(db, mode, "Dhan access token expired")
            db.commit()

    return gate





# ── Routes: health / status ─────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "service": config.SERVICE_NAME, "phase": "1"}


@app.get("/status/{mode}")
async def gate_status(mode: str, db: Session = Depends(get_db)):
    """Public-ish (no admin auth) — the dashboard needs to render 🔴/🟢
    badges before login. Returns only booleans/timestamps, never anything
    secret."""
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)
    account = db.query(models.TradeAccount).filter_by(mode=mode).first()
    risk = db.query(models.TradeRiskConfig).filter_by(mode=mode).first()
    return {
        "mode": mode,
        "admin_authenticated": gate.admin_authenticated,
        "dhan_connected": gate.dhan_connected if mode == "REAL" else None,
        "risk_config_confirmed": gate.risk_config_confirmed,
        "armed": gate.armed,
        "disarmed_reason": gate.disarmed_reason,
        "auto_pilot_enabled": gate.auto_pilot_enabled,
        "account": {
            "starting_capital": account.starting_capital if account else None,
            "current_equity": account.current_equity if account else None,
            "cash_available": account.cash_available if account else None,
            "realized_pnl_today": account.realized_pnl_today if account else None,
        },
        "risk_config": {
            "risk_per_trade_pct": risk.risk_per_trade_pct if risk else None,
            "max_daily_loss_pct": risk.max_daily_loss_pct if risk else None,
            "max_concurrent_positions": risk.max_concurrent_positions if risk else None,
            "max_portfolio_risk_pct": risk.max_portfolio_risk_pct if risk else None,
            # Previously computed server-side but never sent to the frontend,
            # so these three were configurable only via a raw API call —
            # exposing them here lets the dashboard show/edit every field
            # POST /risk-config accepts instead of a fixed subset.
            "stale_data_seconds": risk.stale_data_seconds if risk else None,
            "max_tick_volatility_mult": risk.max_tick_volatility_mult if risk else None,
            "allow_pyramiding": risk.allow_pyramiding if risk else None,
            "updated_at": risk.updated_at.isoformat() if risk and risk.updated_at else None,
            "updated_by": risk.updated_by if risk else None,
        } if risk else None,
    }


# ── Routes: Layer 1 auth (gate 1) ───────────────────────────────────────────
@app.post("/auth/login")
async def login(body: LoginRequest, db: Session = Depends(get_db)):
    try:
        ok = verify_admin_password(body.username, body.password)
    except AdminAuthError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not ok:
        log_action(db, actor=body.username or "unknown", action="ADMIN_LOGIN_FAILED")
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    token, expires_at = issue_session_token(body.username)
    # REAL only — DEMO's gate is permanently pre-authenticated (see
    # _seed_defaults) and must never have its admin_session_expires_at
    # overwritten by a login/logout cycle, or DEMO would start expiring
    # again the first time someone logs in for REAL.
    gate = _gate(db, "REAL")
    gate.admin_authenticated = True
    gate.admin_authenticated_at = datetime.now(timezone.utc)
    gate.admin_session_expires_at = expires_at
    db.commit()
    log_action(db, actor=body.username, action="ADMIN_LOGIN")
    return {"token": token, "expires_at": expires_at.isoformat()}


@app.post("/auth/logout")
async def logout(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    # REAL only — see the matching note in login(). DEMO has no session to
    # log out of; logging out of REAL never touches DEMO's always-on gate.
    gate = _gate(db, "REAL")
    gate.admin_authenticated = False
    if gate.armed:
        _disarm(db, "REAL", "Admin logged out", actor=admin)
    db.commit()
    log_action(db, actor=admin, action="ADMIN_LOGOUT")
    return {"ok": True}


# ── Routes: Layer 2 auth — Dhan connect (gate 2, REAL mode only) ───────────
@app.post("/dhan/connect")
async def connect_dhan(body: ConnectDhanRequest, admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    row = dhan_credentials.save_credentials(db, body.client_id, body.access_token)
    gate = _gate(db, "REAL")
    gate.dhan_connected = True
    gate.dhan_connected_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, actor=admin, action="DHAN_CONNECTED", detail=row.dhan_client_id_masked, mode="REAL")
    return dhan_credentials.connection_status(db)


@app.get("/dhan/status")
async def dhan_status(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    return dhan_credentials.connection_status(db)


@app.get("/dhan/funds")
async def dhan_funds(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Read-only passthrough — proves the connection actually works before
    the admin bothers arming anything."""
    from execution import dhan_client
    try:
        return dhan_client.get_funds(db)
    except dhan_client.DhanNotConnectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.warning("dhan get_funds failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Dhan API error: {e}")


@app.get("/dhan/account")
async def dhan_account(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """One call for the dashboard's Dhan Account card: connection/token
    state (always available, DB-only) plus a live funds call (proves the
    token actually still works against Dhan right now, not just that it
    hasn't expired on paper). Funds failing doesn't hide the connection
    state — a dashboard should be able to show 'connected but Dhan call
    failed: <reason>' rather than going blank."""
    from execution import dhan_client
    status = dhan_credentials.connection_status(db)
    funds = None
    funds_error = None
    if status["connected"]:
        try:
            funds = dhan_client.get_funds(db)
        except Exception as e:
            funds_error = str(e)[:300]
    return {**status, "funds": funds, "funds_error": funds_error}


# ── Routes: risk config (gate 3) ────────────────────────────────────────────
@app.post("/risk-config")
async def update_risk_config(body: RiskConfigUpdate, authorization: str = Header(default=""), db: Session = Depends(get_db)):
    mode = body.mode.upper()
    # mode lives in the request body here, not the path, so it can't be
    # resolved via Depends(require_admin_if_real) the way the path-mode
    # routes are — same enforcement, called manually instead.
    admin = require_admin_if_real(mode, authorization)
    gate = _gate(db, mode)
    if gate.armed:
        raise HTTPException(status_code=409, detail="Cannot change risk config while armed — disarm first.")
    risk = db.query(models.TradeRiskConfig).filter_by(mode=mode).first()
    if risk is None:
        raise HTTPException(status_code=404, detail=f"No risk config row for mode={mode}")

    for field in (
        "risk_per_trade_pct", "max_daily_loss_pct", "max_concurrent_positions",
        "max_portfolio_risk_pct", "stale_data_seconds", "max_tick_volatility_mult",
        "allow_pyramiding",
    ):
        val = getattr(body, field)
        if val is not None:
            setattr(risk, field, val)
    risk.updated_by = admin or "demo-user"
    risk.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, actor=admin or "demo-user", action="RISK_CONFIG_UPDATED", mode=mode, detail=str(body.model_dump(exclude_none=True)))
    return {"ok": True}


@app.post("/risk-config/{mode}/confirm")
async def confirm_risk_config(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    gate = _gate(db, mode)
    gate.risk_config_confirmed = True
    gate.risk_config_confirmed_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, actor=admin, action="RISK_CONFIG_CONFIRMED", mode=mode)
    return {"ok": True}


# ── Routes: arm / disarm (gate 4) ───────────────────────────────────────────
@app.post("/arm/{mode}")
async def arm(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)

    # DEMO: no gate checks at all — _seed_defaults() seeds DEMO's
    # admin_authenticated/risk_config_confirmed as permanently True and
    # _check_and_expire_gates() never touches DEMO's admin_authenticated
    # (see that function), so `missing` is guaranteed empty for DEMO. Only
    # REAL walks the full admin -> dhan -> risk-config sequence.
    missing = []
    if mode == "REAL":
        if not gate.admin_authenticated:
            missing.append("admin_authenticated")
        if not gate.dhan_connected:
            missing.append("dhan_connected")
        if not gate.risk_config_confirmed:
            missing.append("risk_config_confirmed")
    if missing:
        raise HTTPException(status_code=409, detail=f"Cannot arm — missing gates: {', '.join(missing)}")

    gate.armed = True
    gate.armed_at = datetime.now(timezone.utc)
    gate.disarmed_reason = None
    db.commit()
    log_action(db, actor=admin or "demo-user", action="ARMED", mode=mode)
    return {"ok": True, "armed": True, "mode": mode}


@app.post("/disarm/{mode}")
async def disarm(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    _disarm(db, mode, "Manual disarm by admin", actor=admin)
    return {"ok": True, "armed": False, "mode": mode}


@app.post("/emergency-pause")
async def emergency_pause(db: Session = Depends(get_db)):
    """Pauses BOTH modes at once — the 🛑 PAUSE NEW TRADES button.
    Deliberately UNAUTHENTICATED: a stop/pause action only ever reduces
    risk, never increases it, so gating it behind admin login would be
    actively dangerous UX (the one moment you most want to hit this
    button is exactly the moment you don't want a login screen in the
    way). Does not touch open positions/pending orders (that's the
    separate 🚨 EMERGENCY CLOSE ALL action — requires the exit engine,
    which exists now, but closing everything immediately is a bigger,
    deliberate action left for a dedicated confirm-gated route rather
    than folded into this one)."""
    for mode in ("DEMO", "REAL"):
        _disarm(db, mode, "Emergency pause triggered", actor="anonymous")
    return {"ok": True, "paused": True}


# ── Routes: risk engine self-test (no live order path exists yet — this is
#    how the risk engine gets exercised/verified in Phase 1) ───────────────
class RiskCheckRequest(BaseModel):
    mode: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    stop_price: float


@app.post("/risk-engine/check")
async def risk_engine_check(body: RiskCheckRequest, authorization: str = Header(default=""), db: Session = Depends(get_db)):
    """Dry-run the risk engine against current account/risk-config state
    for a hypothetical order — lets the admin verify the 9 checks behave
    as expected BEFORE entry_engine exists to call this for real."""
    mode = body.mode.upper()
    require_admin_if_real(mode, authorization)  # DEMO: open; REAL: admin required
    gate = _check_and_expire_gates(db, mode)
    account_row = db.query(models.TradeAccount).filter_by(mode=mode).first()
    risk_row = db.query(models.TradeRiskConfig).filter_by(mode=mode).first()
    if account_row is None or risk_row is None:
        raise HTTPException(status_code=404, detail=f"No account/risk-config for mode={mode}")

    open_positions = _pf_open_positions(db, mode)
    account_state = AccountState(
        equity=account_row.current_equity,
        risk_per_trade_pct=risk_row.risk_per_trade_pct,
        max_daily_loss_pct=risk_row.max_daily_loss_pct,
        max_concurrent_positions=risk_row.max_concurrent_positions,
        max_portfolio_risk_pct=risk_row.max_portfolio_risk_pct,
        stale_data_seconds=risk_row.stale_data_seconds,
        max_tick_volatility_mult=risk_row.max_tick_volatility_mult,
        allow_pyramiding=risk_row.allow_pyramiding,
        realized_pnl_today=account_row.realized_pnl_today,
        open_position_count=len(open_positions),
        open_position_symbols={p.symbol for p in open_positions},
        open_positions_total_risk=sum(
            abs(p.avg_entry_price - (p.current_stop or p.avg_entry_price)) * p.qty_open
            for p in open_positions
        ),
        trading_globally_paused=not gate.armed,
        market_is_open=True,  # Phase 2: wire to market_feed's real market-hours check
    )
    intent = OrderIntent(
        mode=mode, symbol=body.symbol.upper(), side=body.side.upper(),
        qty=body.qty, entry_price=body.entry_price, stop_price=body.stop_price,
    )
    result = risk_evaluate(intent, account_state)
    db.add(models.TradeRiskEvent(
        mode=mode, symbol=body.symbol.upper(), check_name=result.check_name,
        verdict=result.verdict.value, detail=result.reason,
    ))
    db.commit()
    return {
        "verdict": result.verdict.value,
        "check_name": result.check_name,
        "reason": result.reason,
        "approved_qty": result.approved_qty,
    }


class ManualOrderRequest(BaseModel):
    symbol: str
    side: str                              # "BUY" | "SELL"
    qty: int
    order_type: str = "LIMIT"              # "LIMIT" | "MARKET"
    limit_price: Optional[float] = None    # required for LIMIT; ignored for MARKET (priced off the live tick)
    product_type: str = "CNC"              # "CNC" | "MIS"
    stop_price: Optional[float] = None     # BUY only; a conservative flat default is used if omitted
    target_price: Optional[float] = None   # BUY only; same
    position_id: Optional[int] = None      # SELL only; if omitted, resolves the open position by symbol


# ── Routes: Manual Execution Gateway (Stockky Trade ticket) ────────────────
# Two-step by design — see manual_engine.py's module docstring. The
# frontend's "Review Order" screen calls /preview (no DB writes); the
# "Confirm BUY/SELL" button calls /confirm, which re-derives everything
# from current state rather than trusting the preview response. Both
# routes share the exact same gate/risk path DEMO and REAL orders always
# have — a manual mistake (fat-fingered qty, stale stop) is blocked by the
# same risk engine an automatic candidate would be.
@app.post("/manual-order/{mode}/preview")
async def manual_order_preview(
    mode: str, body: ManualOrderRequest,
    admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db),
):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)
    from manual_engine import evaluate_manual_order
    return await evaluate_manual_order(db, mode, gate.armed, body, confirm=False, admin=admin)


@app.post("/manual-order/{mode}/confirm")
async def manual_order_confirm(
    mode: str, body: ManualOrderRequest,
    admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db),
):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)
    if body.side.upper() == "BUY" and not gate.armed:
        # SELL is intentionally exempt (see manual_engine.py) — exiting a
        # position must never be blocked by "not armed", same policy as
        # the existing manual-close endpoint. A BUY, however, is new
        # risk-taking and must go through the same "armed" gate as
        # everything else that can open a position.
        raise HTTPException(status_code=409, detail=f"{mode} is not armed — arm it before sending a manual BUY.")
    from manual_engine import evaluate_manual_order
    return await evaluate_manual_order(db, mode, gate.armed, body, confirm=True, admin=admin)


class ManualCandidateRequest(BaseModel):
    symbol: str
    decision_label: Optional[str] = None
    conviction_score: Optional[float] = None
    signal_price: Optional[float] = None


# ── Routes: manual candidate injection from the Scan Market page ───────────
@app.post("/candidates/manual/{mode}")
async def add_manual_candidate(
    mode: str,
    body: ManualCandidateRequest,
    admin: Optional[str] = Depends(require_admin_if_real),
    db: Session = Depends(get_db),
):
    """Lets the main Scan Market page push one specific stock straight into
    this mode's candidate queue — same table entry_engine already reads
    from (source_tab='market_scan'), so the very next Run Cycle (or the
    one already in flight) evaluates it exactly like an auto-fed Hot
    Picks/IPO candidate. Does not place an order itself — only queues the
    candidate for the normal risk-checked entry path."""
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)
    if not gate.armed:
        raise HTTPException(status_code=409, detail=f"{mode} is not armed — arm it before sending candidates.")

    symbol = (body.symbol or "").upper().strip().replace(".NS", "").replace(".BO", "")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    row = models.TradeCandidate(
        mode=mode,
        symbol=symbol,
        source_tab="market_scan",
        decision_label=body.decision_label,
        conviction_score=body.conviction_score,
        signal_price=body.signal_price,
    )
    db.add(row)
    db.commit()
    log_action(db, actor=admin or "admin", action="MANUAL_CANDIDATE", mode=mode,
               detail=f"{symbol} queued from Scan Market page")
    return {"ok": True, "mode": mode, "symbol": symbol, "queued": True}


# ── Routes: audit log read ───────────────────────────────────────────────────
# ── Routes: Phase 2 — DEMO cycle (candidates -> entry -> fills -> exits) ───
@app.post("/cycle/run/{mode}")
async def run_cycle(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    """Runs one full evaluation cycle: refresh candidates from api-gateway,
    evaluate entries (risk-checked), check pending order fills, expire
    stale unfilled orders, then evaluate exits on every open position.
    DEMO mode does all of this end-to-end including simulated fills. REAL
    mode places/exits real orders through Dhan (Phase 3,
    execution/dhan_client.py) and reconciles confirmed fills via
    execution/reconcile.py — reconciliation runs every REAL cycle right
    alongside DEMO's check_pending_fills, in the same slot.

    This is a manual trigger, not a scheduler — see the module note in
    startup() for why continuous background scheduling isn't wired yet."""
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _check_and_expire_gates(db, mode)
    if not gate.armed:
        raise HTTPException(status_code=409, detail=f"{mode} is not armed — arm it before running a cycle.")

    from cycle_runner import run_cycle_core
    return await run_cycle_core(db, mode, gate.armed, trigger="manual")


# ── Routes: Pipeline dashboard (2026-08-27) ──────────────────────────────────
# Read-only status for the Pipeline tab: what stage the current cycle (manual
# OR auto-pilot) is in right now, which symbol/source it's on, per-stage
# timing, and a short history of recent cycles. Backed entirely by
# pipeline_status.py's in-memory tracker — never touches trading state, so
# this is safe to poll as often as the dashboard wants.
@app.get("/pipeline/status/{mode}")
async def pipeline_status_route(mode: str, admin: Optional[str] = Depends(require_admin_if_real)):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    import pipeline_status as pstat
    return pstat.get_status(mode)


# ── Routes: Auto-Pilot (2026-08-27) ──────────────────────────────────────────
# Toggle only — the actual loop lives in execution/auto_pilot.py, started
# once at app startup and running for the lifetime of the process. Turning
# this on does NOT bypass arming: the loop re-checks `armed` (and
# auto_pilot_enabled) fresh from the DB on every tick, so an auto-disarm
# (session/token expiry, daily loss cap, emergency pause) stops it exactly
# like it stops a manual Run Cycle click.
@app.post("/autopilot/{mode}/enable")
async def autopilot_enable(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _gate(db, mode)
    gate.auto_pilot_enabled = True
    gate.auto_pilot_enabled_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, actor=admin or "demo-user", action="AUTOPILOT_ENABLED", mode=mode)
    return {"ok": True, "mode": mode, "auto_pilot_enabled": True}


@app.post("/autopilot/{mode}/disable")
async def autopilot_disable(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")
    gate = _gate(db, mode)
    gate.auto_pilot_enabled = False
    db.commit()
    log_action(db, actor=admin or "demo-user", action="AUTOPILOT_DISABLED", mode=mode)
    return {"ok": True, "mode": mode, "auto_pilot_enabled": False}


async def _live_prices(symbols: list[str]) -> dict[str, float]:
    """Best-effort LTP lookup for dashboard display only — NEVER used to
    size, price, or evaluate an actual order (entry_engine/exit_engine call
    market_feed/dhan_client directly for that). A quote failing here just
    means the dashboard shows '—' for current price; it must never raise
    and never block the positions/orders/candidates list from returning."""
    if not symbols:
        return {}
    try:
        from market_feed.feed import get_quotes
        ticks = await get_quotes(list(dict.fromkeys(symbols)))  # de-dupe, preserve order
        return {sym: t.price for sym, t in ticks.items() if t is not None}
    except Exception as e:
        logger.warning("live price lookup failed (display-only, non-fatal): %s", e)
        return {}


@app.get("/positions/{mode}")
async def list_positions(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    rows = (
        db.query(models.TradePosition)
        .filter(models.TradePosition.mode == mode,
                models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED", "PENDING_EXIT")))
        .all()
    )  # includes PENDING_EXIT (a REAL exit already sent to Dhan, awaiting confirmation) — the DEMO/entry-cycle
       # open_positions() helper deliberately excludes it from re-evaluation; this endpoint should still show it.
    prices = await _live_prices([p.symbol for p in rows])
    out = []
    for p in rows:
        ltp = prices.get(p.symbol)
        pnl_pct = None
        stop_distance_pct = None
        target_distance_pct = None
        if ltp is not None and p.avg_entry_price:
            pnl_pct = round((ltp - p.avg_entry_price) / p.avg_entry_price * 100.0, 2)
        if ltp is not None and p.current_stop:
            stop_distance_pct = round((ltp - p.current_stop) / ltp * 100.0, 2)
        if ltp is not None and p.current_target:
            target_distance_pct = round((p.current_target - ltp) / ltp * 100.0, 2)
        out.append({
            "id": p.id, "symbol": p.symbol, "status": p.status, "qty_open": p.qty_open,
            "avg_entry_price": p.avg_entry_price, "current_stop": p.current_stop,
            "current_target": p.current_target, "unrealized_pnl": p.unrealized_pnl,
            "realized_pnl": p.realized_pnl, "opened_at": p.opened_at.isoformat(),
            "current_price": ltp,
            "pnl_pct": pnl_pct,
            # How close LTP is to knocking the stop/target, as a % of current price —
            # lets the dashboard show a live "X% above stop / Y% below target" readout
            # instead of just the raw levels, without duplicating exit_engine's own logic.
            "stop_distance_pct": stop_distance_pct,
            "target_distance_pct": target_distance_pct,
        })
    return out


@app.get("/orders/{mode}")
async def list_orders(mode: str, limit: int = 50, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    mode = mode.upper()
    rows = (
        db.query(models.TradeOrder).filter_by(mode=mode)
        .order_by(models.TradeOrder.created_at.desc()).limit(min(max(limit, 1), 200)).all()
    )
    # Only bother pricing orders still "in flight" (waiting on a limit fill) —
    # a FILLED/CANCELLED/REJECTED/EXPIRED order's current price is irrelevant
    # dashboard noise, and skipping them keeps the live-price call small.
    live_symbols = [o.symbol for o in rows if o.status in ("PENDING", "PLACED")]
    prices = await _live_prices(live_symbols)
    out = []
    for o in rows:
        ltp = prices.get(o.symbol)
        limit_distance_pct = None
        if ltp is not None and o.limit_price and o.status in ("PENDING", "PLACED"):
            # Positive = LTP still above the BUY limit (waiting for price to
            # come down to fill); negative = LTP has already crossed it.
            limit_distance_pct = round((ltp - o.limit_price) / o.limit_price * 100.0, 2)
        out.append({
            "id": o.id, "symbol": o.symbol, "side": o.side, "qty": o.qty, "order_type": o.order_type,
            "limit_price": o.limit_price, "status": o.status,
            "valid_until": o.valid_until.isoformat() if o.valid_until else None,
            "created_at": o.created_at.isoformat(),
            "execution_source": o.execution_source,
            "current_price": ltp,
            "limit_distance_pct": limit_distance_pct,
        })
    return out


@app.get("/candidates/{mode}")
async def list_candidates(
    mode: str, limit: int = 40,
    admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db),
):
    """Dashboard visibility into 'what the engine is actually looking at' —
    the piece that was previously invisible between a cycle running and an
    order appearing. Shows every recently-fetched candidate joined with
    entry_engine's latest ENTRY decision for that same (mode, symbol) pair
    (if any yet), plus a live price so 'waiting at limit ₹X, currently ₹Y'
    is visible without cross-referencing the Orders tab. Read-only —
    exactly like /pipeline/status, this never influences entry_engine's own
    evaluation, which reads trade_candidates itself, not this endpoint."""
    mode = mode.upper()
    if mode not in ("DEMO", "REAL"):
        raise HTTPException(status_code=400, detail="mode must be DEMO or REAL")

    candidates = (
        db.query(models.TradeCandidate).filter_by(mode=mode)
        .order_by(models.TradeCandidate.received_at.desc())
        .limit(min(max(limit, 1), 200)).all()
    )
    symbols = [c.symbol for c in candidates]
    prices = await _live_prices(symbols)

    # Latest ENTRY decision per symbol (one query, not N+1) — gives each
    # candidate its most recent WAIT/ENTER verdict + reasoning + the
    # limit/stop/target the risk engine proposed for it, if it has been
    # evaluated at least once.
    latest_decisions: dict[str, models.TradeDecision] = {}
    if symbols:
        decision_rows = (
            db.query(models.TradeDecision)
            .filter(models.TradeDecision.mode == mode,
                    models.TradeDecision.decision_type == "ENTRY",
                    models.TradeDecision.symbol.in_(set(symbols)))
            .order_by(models.TradeDecision.created_at.desc())
            .all()
        )
        for d in decision_rows:
            if d.symbol not in latest_decisions:
                latest_decisions[d.symbol] = d  # first hit per symbol = most recent (already ordered desc)

    out = []
    for c in candidates:
        d = latest_decisions.get(c.symbol)
        ltp = prices.get(c.symbol)
        limit_distance_pct = None
        if ltp is not None and d and d.proposed_price and d.action == "WAIT":
            limit_distance_pct = round((ltp - d.proposed_price) / d.proposed_price * 100.0, 2)
        out.append({
            "id": c.id, "symbol": c.symbol, "source_tab": c.source_tab,
            "decision_label": c.decision_label, "conviction_score": c.conviction_score,
            "signal_price": c.signal_price, "received_at": c.received_at.isoformat(),
            "consumed": c.consumed,
            "current_price": ltp,
            "latest_decision": {
                "action": d.action, "reasoning": d.reasoning,
                "proposed_qty": d.proposed_qty, "proposed_price": d.proposed_price,
                "proposed_stop": d.proposed_stop, "proposed_target": d.proposed_target,
                "risk_verdict": d.risk_verdict, "risk_verdict_reason": d.risk_verdict_reason,
                "evaluated_at": d.created_at.isoformat(),
                "limit_distance_pct": limit_distance_pct,
            } if d else None,
        })
    return out


class ManualCloseRequest(BaseModel):
    qty: Optional[int] = None  # None = close the whole open qty


@app.post("/positions/{mode}/{position_id}/close")
async def manual_close_position(
    mode: str, position_id: int, body: ManualCloseRequest = ManualCloseRequest(),
    admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db),
):
    """Manual override — close a position right now, independent of
    whether the automatic exit cycle would currently trigger on it.
    Always allowed regardless of armed state, same policy as
    dhan_client.cancel_order: exiting a position is never something the
    system should refuse just because trading is disarmed."""
    mode = mode.upper()
    position = db.query(models.TradePosition).filter_by(id=position_id, mode=mode).first()
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status not in ("OPEN", "PARTIALLY_CLOSED"):
        raise HTTPException(status_code=409, detail=f"Position is {position.status} — nothing to close.")

    qty = body.qty or position.qty_open
    qty = min(qty, position.qty_open)
    if qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")

    if mode == "DEMO":
        from market_feed.feed import get_quotes
        ticks = await get_quotes([position.symbol])
        tick = ticks.get(position.symbol)
        if tick is None:
            raise HTTPException(status_code=503, detail=f"No current price available for {position.symbol} — try again shortly.")
        pnl = _pf_close_position(db, position, tick, qty, "manual_close")
        log_action(db, actor=admin or "admin", action="MANUAL_CLOSE", mode=mode,
                   detail=f"{position.symbol} qty={qty} pnl={pnl:+.2f}")
        return {"ok": True, "mode": mode, "symbol": position.symbol, "qty_closed": qty, "pnl": pnl}
    else:
        from exit_engine.exit import _send_real_sell
        full = qty >= position.qty_open
        if not _send_real_sell(db, position, qty, "manual_close", full=full):
            raise HTTPException(status_code=502, detail="Dhan rejected the manual close order — see server logs.")
        log_action(db, actor=admin or "admin", action="MANUAL_CLOSE_SENT", mode=mode,
                   detail=f"{position.symbol} qty={qty} sent to Dhan, awaiting confirmation")
        return {"ok": True, "mode": mode, "symbol": position.symbol, "qty_sent": qty, "status": "pending_broker_confirmation"}


@app.post("/orders/{mode}/{order_id}/cancel")
async def manual_cancel_order(
    mode: str, order_id: int,
    admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db),
):
    """Manual override — cancel a still-PLACED order. Always allowed
    regardless of armed state (matches dhan_client.cancel_order's own
    policy: backing out of a pending order is never gated by arming)."""
    mode = mode.upper()
    order = db.query(models.TradeOrder).filter_by(id=order_id, mode=mode).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != "PLACED":
        raise HTTPException(status_code=409, detail=f"Order is {order.status} — nothing to cancel.")

    if mode == "REAL" and order.dhan_order_id:
        try:
            dhan_client.cancel_order(db, is_armed=True, dhan_order_id=order.dhan_order_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Dhan rejected the cancel: {e}")

    order.status = "CANCELLED"
    order.updated_at = datetime.now(timezone.utc)
    db.add(models.TradeOrderEvent(order_id=order.id, event_type="CANCELLED", detail="Manually cancelled by admin"))
    db.commit()
    log_action(db, actor=admin or "admin", action="MANUAL_CANCEL", mode=mode, detail=f"order {order.id} ({order.symbol})")
    return {"ok": True, "mode": mode, "order_id": order.id, "status": "CANCELLED"}


@app.post("/reconcile/{mode}")
async def manual_reconcile(mode: str, admin: Optional[str] = Depends(require_admin_if_real), db: Session = Depends(get_db)):
    """Manual trigger for the same broker reconciliation that also runs
    automatically at the end of every REAL Run Cycle — exposed on its own
    so a stuck/PLACED order can be re-checked against Dhan without waiting
    for (or re-running) a full cycle."""
    mode = mode.upper()
    if mode != "REAL":
        return {"ok": True, "mode": mode, "note": "DEMO has nothing to reconcile — fills are simulated, not broker-confirmed."}
    from execution.reconcile import reconcile_real_orders
    result = await reconcile_real_orders(db)
    return {"ok": True, "mode": mode, **result}


@app.get("/audit-log")
async def audit_log(mode: Optional[str] = None, limit: int = 50, authorization: str = Header(default=""), db: Session = Depends(get_db)):
    # mode=None (all modes) or mode=REAL both require admin, since either
    # could surface REAL-mode activity; only an explicit mode=DEMO query
    # is open. Manual check (not Depends) since mode is optional here and
    # "no mode" must still resolve to "treat as REAL for auth purposes".
    require_admin_if_real(mode or "REAL", authorization)
    q = db.query(models.TradeAuditLog)
    if mode:
        q = q.filter_by(mode=mode.upper())
    rows = q.order_by(models.TradeAuditLog.occurred_at.desc()).limit(min(max(limit, 1), 200)).all()
    return [
        {"actor": r.actor, "action": r.action, "detail": r.detail, "mode": r.mode, "occurred_at": r.occurred_at.isoformat()}
        for r in rows
    ]


# ── Routes: Live Dhan account data (read-only, admin-gated) ─────────────────

@app.get("/dhan/positions")
async def dhan_live_positions(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Live intraday + CNC positions from Dhan broker — not the service's own
    trade_positions table. Used by the dashboard to show what's actually
    open at the broker level, independent of reconciliation state."""
    try:
        positions = dhan_client.get_positions(db)
        return {"ok": True, "positions": positions}
    except dhan_client.DhanNotConnectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.warning("dhan get_positions failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Dhan API error: {e}")


@app.get("/dhan/holdings")
async def dhan_live_holdings(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Live demat holdings from Dhan."""
    try:
        holdings = dhan_client.get_holdings(db)
        return {"ok": True, "holdings": holdings}
    except dhan_client.DhanNotConnectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.warning("dhan get_holdings failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Dhan API error: {e}")


@app.get("/dhan/orders")
async def dhan_live_orders(admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Live today's order list from Dhan broker."""
    try:
        orders = dhan_client.get_order_list(db)
        return {"ok": True, "orders": orders}
    except dhan_client.DhanNotConnectedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.warning("dhan get_order_list failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Dhan API error: {e}")
