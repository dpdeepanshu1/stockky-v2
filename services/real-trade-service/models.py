"""
models.py — trade_* schema for Real Automatic Trade.

Standard SQLAlchemy column types only (String/Integer/Float/Boolean/
DateTime/Text) — no hand-written per-dialect DDL needed here (unlike
oracle_compat.py's KV table) because SQLAlchemy's Core DDL compiler already
knows how to render every one of these correctly for both the oracledb and
psycopg2 dialects. `mode` (DEMO/REAL) is part of the primary/lookup key on
every trade-bearing table on purpose — see the module note in db.py: paper
and real data must never be reachable through the same query without
explicitly asking for both.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _now():
    return datetime.now(timezone.utc)


# ── Gate / arming state machine ─────────────────────────────────────────────
class TradeGateState(Base):
    """Singleton-per-mode row tracking the 4-gate arming sequence:
    admin_authenticated -> dhan_connected -> risk_config_confirmed -> armed.
    Each gate carries its own timestamp so any one of them can expire
    independently (admin session idle-timeout, Dhan token 24h expiry) and
    auto-disarm without touching the others' history."""
    __tablename__ = "trade_gate_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True)  # "DEMO" | "REAL"

    admin_authenticated = Column(Boolean, nullable=False, default=False)
    admin_authenticated_at = Column(DateTime, nullable=True)
    admin_session_expires_at = Column(DateTime, nullable=True)

    dhan_connected = Column(Boolean, nullable=False, default=False)
    dhan_connected_at = Column(DateTime, nullable=True)

    risk_config_confirmed = Column(Boolean, nullable=False, default=False)
    risk_config_confirmed_at = Column(DateTime, nullable=True)

    armed = Column(Boolean, nullable=False, default=False)
    armed_at = Column(DateTime, nullable=True)
    disarmed_reason = Column(String(255), nullable=True)  # last disarm cause, for the UI

    # Auto-Pilot (2026-08-27) — independent of `armed`. Arming only means
    # "this mode is allowed to trade right now if something triggers it"
    # (a manual Run Cycle click, or auto-pilot). auto_pilot_enabled is the
    # separate "keep running cycles on a timer, unattended" switch — never
    # implied by armed, and always re-checked against armed at tick time,
    # so disarming (including an automatic disarm on token/session expiry)
    # always stops auto-pilot from placing anything, even if the toggle
    # itself is left on.
    auto_pilot_enabled = Column(Boolean, nullable=False, default=False)
    auto_pilot_enabled_at = Column(DateTime, nullable=True)

    # ── Scheduled automation (2026-08-31) — three independent, default-OFF
    # features layered ON TOP of auto-pilot. Each runs when this per-mode DB
    # toggle is on (the SOLE on/off authority since 2026-09-01's env-gate
    # removal — see config.py), the mode is armed, and (except pre-pick,
    # which runs pre-open) the market is open. The *_last_run columns hold the
    # IST date ('YYYY-MM-DD') the action last fired, so each fires at most once
    # per trading day even across process restarts (Render wipes memory).
    prepick_enabled = Column(Boolean, nullable=False, default=False)
    prepick_enabled_at = Column(DateTime, nullable=True)
    prepick_last_run = Column(String(10), nullable=True)

    enter_at_open_enabled = Column(Boolean, nullable=False, default=False)
    enter_at_open_enabled_at = Column(DateTime, nullable=True)
    enter_at_open_last_run = Column(String(10), nullable=True)

    eod_squareoff_enabled = Column(Boolean, nullable=False, default=False)
    eod_squareoff_enabled_at = Column(DateTime, nullable=True)
    eod_squareoff_last_run = Column(String(10), nullable=True)

    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ── Accounts (one row per mode: DEMO paper account, REAL linked Dhan account) ─
class TradeAccount(Base):
    __tablename__ = "trade_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True)  # "DEMO" | "REAL"
    starting_capital = Column(Float, nullable=False, default=100000.0)
    current_equity = Column(Float, nullable=False, default=100000.0)
    cash_available = Column(Float, nullable=False, default=100000.0)
    realized_pnl_today = Column(Float, nullable=False, default=0.0)
    realized_pnl_total = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ── Dhan credentials (REAL mode only; encrypted at rest) ────────────────────
class TradeCredential(Base):
    """One row, REAL mode only. dhan_client_id and access_token_encrypted are
    written by auth/dhan_credentials.py and NEVER read back to the frontend
    — the API only ever returns a masked status. See config.py's
    DHAN_CREDENTIAL_ENC_KEY (Fernet)."""
    __tablename__ = "trade_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dhan_client_id_masked = Column(String(64), nullable=True)   # e.g. "****6789" for display only
    dhan_client_id_encrypted = Column(Text, nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    token_issued_at = Column(DateTime, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)          # Dhan tokens: issued_at + 24h
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ── Risk configuration (admin-editable only while disarmed) ────────────────
class TradeRiskConfig(Base):
    __tablename__ = "trade_risk_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True)
    risk_per_trade_pct = Column(Float, nullable=False, default=1.0)
    max_daily_loss_pct = Column(Float, nullable=False, default=3.0)
    max_concurrent_positions = Column(Integer, nullable=False, default=3)
    max_portfolio_risk_pct = Column(Float, nullable=False, default=5.0)
    stale_data_seconds = Column(Integer, nullable=False, default=30)
    max_tick_volatility_mult = Column(Float, nullable=False, default=2.0)
    allow_pyramiding = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    updated_by = Column(String(64), nullable=True)  # admin username, for audit


# ── Candidates pulled from existing Stockky recommendations ────────────────
class TradeCandidate(Base):
    __tablename__ = "trade_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    symbol = Column(String(32), nullable=False)
    source_tab = Column(String(32), nullable=True)  # "hot_picks" | "surprise" | "ipo" | "market_scan"
    decision_label = Column(String(32), nullable=True)  # e.g. "BUY NOW"
    conviction_score = Column(Float, nullable=True)
    signal_price = Column(Float, nullable=True)
    raw_payload = Column(Text, nullable=True)  # JSON snapshot of the source recommendation
    received_at = Column(DateTime, nullable=False, default=_now)
    consumed = Column(Boolean, nullable=False, default=False)  # entry_engine has evaluated it

    __table_args__ = (Index("ix_trade_candidates_mode_symbol", "mode", "symbol"),)


# ── Entry/exit decisions (the "why", separate from the resulting order) ────
class TradeDecision(Base):
    __tablename__ = "trade_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    candidate_id = Column(Integer, ForeignKey("trade_candidates.id"), nullable=True)
    symbol = Column(String(32), nullable=False)
    decision_type = Column(String(16), nullable=False)  # "ENTRY" | "EXIT"
    action = Column(String(24), nullable=False)          # "WAIT" | "ENTER" | "HOLD" | "TRAIL_STOP" | "PARTIAL_EXIT" | "FULL_EXIT" | "EMERGENCY_EXIT"
    reasoning = Column(Text, nullable=True)
    proposed_qty = Column(Integer, nullable=True)
    proposed_price = Column(Float, nullable=True)
    proposed_stop = Column(Float, nullable=True)
    proposed_target = Column(Float, nullable=True)
    risk_verdict = Column(String(24), nullable=True)     # "APPROVED" | "REJECTED" | "BLOCKED_GLOBAL"
    risk_verdict_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


# ── Orders / order lifecycle / fills ────────────────────────────────────────
class TradeOrder(Base):
    __tablename__ = "trade_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    decision_id = Column(Integer, ForeignKey("trade_decisions.id"), nullable=True)
    symbol = Column(String(32), nullable=False)
    side = Column(String(4), nullable=False)              # "BUY" | "SELL"
    order_type = Column(String(16), nullable=False, default="LIMIT")
    qty = Column(Integer, nullable=False)
    limit_price = Column(Float, nullable=True)
    valid_until = Column(DateTime, nullable=True)          # time-boxed entry window (decision 1)
    status = Column(String(16), nullable=False, default="PENDING")  # PENDING/PLACED/FILLED/PARTIAL/CANCELLED/REJECTED/EXPIRED
    dhan_order_id = Column(String(64), nullable=True)      # null in DEMO mode
    # Who/what originated this order — lets the dashboard (and later,
    # Training) answer "manual vs automatic" without inferring it from
    # decision_id being null. "AUTO" is the default so every existing row
    # and every entry_engine/exit_engine-created order keeps its original
    # meaning with zero migration risk; only manual_engine.py ever writes
    # "MANUAL". See db.py's _ensure_manual_order_columns for the additive
    # migration that adds this column to an already-deployed table.
    execution_source = Column(String(16), nullable=False, default="AUTO")  # "AUTO" | "MANUAL" | "EXIT"
    confirmed_by = Column(String(64), nullable=True)   # admin username who hit "Confirm" (MANUAL real-money orders only)
    confirmed_at = Column(DateTime, nullable=True)
    # Our OWN reason for a SELL ("stop_hit" | "target_hit_partial" | "time_stop"
    # | "emergency_exit" | "manual"), set at send time in exit_engine._send_real_sell.
    # NOT the broker's remarks field — reconcile.py previously read Dhan's own
    # `remarks` as the "reason" for a confirmed exit fill, which is broker
    # text (often blank or unrelated), not our trading logic's reason. This
    # column is what record_real_exit_fill and the partial-exit
    # breakeven-stop logic key off of instead (see reconcile.py, 2026-08-27).
    exit_reason = Column(String(32), nullable=True)
    # Cumulative qty this order has ever had booked into a TradeFill /
    # position / account by reconcile_real_orders — NOT the same as
    # TradeFill rows summed (kept as its own column so reconcile can do a
    # cheap "how much is new since last check" comparison without a
    # second query every pass). Dhan's filledQty on a PART_TRADED or
    # TRADED order is always the order's cumulative filled quantity to
    # date, never a per-poll increment, so reconcile diffs against this
    # column to book only the NEW shares each pass instead of re-booking
    # the whole cumulative amount every time it sees the same order.
    # Stays 0 for DEMO orders (which never go through reconcile) and for
    # any REAL order that hasn't had a broker-confirmed fill yet. See
    # db.py's _ensure_manual_order_columns for the additive migration
    # that adds this column to an already-deployed table.
    filled_qty_so_far = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class TradeOrderEvent(Base):
    """Append-only order status transitions — one row per state change,
    never mutated, so the full lifecycle can always be replayed."""
    __tablename__ = "trade_order_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("trade_orders.id"), nullable=False)
    event_type = Column(String(32), nullable=False)  # "PLACED" | "MODIFIED" | "FILLED" | "PARTIAL_FILL" | "CANCELLED" | "REJECTED" | "EXPIRED"
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=_now)


class TradeFill(Base):
    __tablename__ = "trade_fills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("trade_orders.id"), nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    dhan_trade_id = Column(String(64), nullable=True)  # null in DEMO mode
    filled_at = Column(DateTime, nullable=False, default=_now)


# ── Positions / position lifecycle ──────────────────────────────────────────
class TradePosition(Base):
    __tablename__ = "trade_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    symbol = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="OPEN")  # OPEN | PARTIALLY_CLOSED | CLOSED
    qty_open = Column(Integer, nullable=False, default=0)
    avg_entry_price = Column(Float, nullable=False)
    current_stop = Column(Float, nullable=True)
    current_target = Column(Float, nullable=True)
    # 2026-09-01 fix: fixed once at position-open time (|avg_entry_price -
    # stop_price| from the opening fill) so exit_engine's gap-down
    # emergency-exit check has a stable "original stop distance" to compare
    # against. Previously that check re-derived the distance from
    # current_stop every cycle, which drifts as the trail/breakeven logic
    # moves current_stop — nullable so rows opened before this migration
    # (which have no way to know their original distance) fall back to the
    # old current_stop-based approximation in exit_engine.
    initial_stop_distance = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    opened_at = Column(DateTime, nullable=False, default=_now)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_trade_positions_mode_symbol_status", "mode", "symbol", "status"),)


class TradePositionEvent(Base):
    __tablename__ = "trade_position_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("trade_positions.id"), nullable=False)
    event_type = Column(String(32), nullable=False)  # "OPENED" | "STOP_TRAILED" | "PARTIAL_EXIT" | "CLOSED" | "TIME_STOP"
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=_now)


class TradeExitDecision(Base):
    """Every exit-engine evaluation cycle for an open position, even the
    ones that resulted in HOLD — this is what makes the exit logic
    debuggable rather than a black box (see plan's audit principle)."""
    __tablename__ = "trade_exit_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("trade_positions.id"), nullable=False)
    action = Column(String(24), nullable=False)  # "HOLD" | "TRAIL_STOP" | "PARTIAL_EXIT" | "FULL_EXIT" | "EMERGENCY_EXIT"
    reasoning = Column(Text, nullable=True)
    ltp_at_decision = Column(Float, nullable=True)
    evaluated_at = Column(DateTime, nullable=False, default=_now)


# ── Risk events (every REJECTED/BLOCKED_GLOBAL verdict, for tuning limits) ──
class TradeRiskEvent(Base):
    __tablename__ = "trade_risk_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    symbol = Column(String(32), nullable=True)
    check_name = Column(String(64), nullable=False)  # which of the 9 checks fired
    verdict = Column(String(24), nullable=False)
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=_now)


# ── Daily P&L rollup ─────────────────────────────────────────────────────────
class TradePnl(Base):
    __tablename__ = "trade_pnl"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    trade_date = Column(String(10), nullable=False)  # "YYYY-MM-DD" (IST trading day)
    starting_equity = Column(Float, nullable=False)
    ending_equity = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    trades_count = Column(Integer, nullable=False, default=0)
    win_count = Column(Integer, nullable=False, default=0)
    max_drawdown_pct = Column(Float, nullable=True)

    __table_args__ = (UniqueConstraint("mode", "trade_date", name="uq_trade_pnl_mode_date"),)


# ── Reconciliation (Stockky DB state vs actual Dhan account state) ─────────
class TradeReconciliation(Base):
    __tablename__ = "trade_reconciliation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)
    check_type = Column(String(32), nullable=False)  # "ORDERS" | "POSITIONS" | "FUNDS"
    matched = Column(Boolean, nullable=False)
    discrepancy_detail = Column(Text, nullable=True)
    triggered_safety_lock = Column(Boolean, nullable=False, default=False)
    checked_at = Column(DateTime, nullable=False, default=_now)


# ── Audit log — append-only, every consequential action ────────────────────
class TradeAuditLog(Base):
    __tablename__ = "trade_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=True)  # null for account-level events (login, arm/disarm)
    actor = Column(String(64), nullable=True)  # "admin" | "system" | "risk_engine"
    action = Column(String(64), nullable=False)  # e.g. "ADMIN_LOGIN", "DHAN_CONNECTED", "ARMED", "DISARMED", "ORDER_PLACED"
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (Index("ix_trade_audit_log_action_time", "action", "occurred_at"),)


# ── Market Regime History (for adaptive threshold computation) ───────────────
# Added by adaptive_thresholds.py improvement. One row per market_score reading
# recorded during entry_engine's regime fetch. Pruned automatically to trailing
# ADAPTIVE_HISTORY_DAYS. Provides the data for the 20th-percentile adaptive
# regime gate instead of the frozen static threshold.
class MarketRegimeHistory(Base):
    """Records market_score readings for adaptive regime gate computation."""
    __tablename__ = "market_regime_history"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    score       = Column(Float, nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (Index("ix_market_regime_history_recorded_at", "recorded_at"),)
