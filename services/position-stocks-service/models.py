"""
models.py — position-stocks-service's own tables, ALL prefixed `scalp_` so
they can never collide with real-trade-service's `trade_*` tables in the
same physical database.

This service does NOT define a TradeCredential model — it only ever reads
that table (owned by real-trade-service) via a lightweight, read-only
mapped class kept in auth/dhan_credentials_ro.py, deliberately NOT
included in this Base/metadata so init_tables() here can never attempt to
create/alter a table it doesn't own.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ScalpCapitalLedger(Base):
    """Single-row-per-mode ledger tracking this pool's software-enforced
    50/50 split of total capital. Dhan itself does not segregate a single
    account's funds into pools — this table IS the segregation.
    total_allocated_capital is set once (or updated) from the account's
    real fund balance * SCALP_POOL_CAPITAL_SHARE_PCT; available_capital is
    decremented on entry and incremented back on exit (realized P&L
    applied), same accounting pattern real-trade-service's own portfolio
    ledger uses for its half."""
    __tablename__ = "scalp_capital_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True, default="REAL")
    total_allocated_capital = Column(Float, nullable=False, default=0.0)
    available_capital = Column(Float, nullable=False, default=0.0)
    realized_pnl_today = Column(Float, nullable=False, default=0.0)
    realized_pnl_total = Column(Float, nullable=False, default=0.0)
    last_synced_from_broker_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class ScalpPosition(Base):
    """One row per scalp trade. Separate from real-trade-service's
    TradePosition table by design (§3.4/§4 of the tracking doc) — this
    service must never share a live SQLAlchemy model/table with a system
    it's supposed to be fully isolated from."""
    __tablename__ = "scalp_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    dhan_security_id = Column(String(32), nullable=False)
    window_source = Column(String(8), nullable=False)  # "5m" | "15m" | "60m"
    status = Column(String(24), nullable=False, default="OPEN", index=True)
    # OPEN, TARGET_HIT, STOP_HIT, EOD_SQUAREOFF, MANUAL_EXIT, ERROR

    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    target_price = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=False)
    adaptive_target_pct = Column(Float, nullable=False)
    adaptive_stop_pct = Column(Float, nullable=False)

    dhan_super_order_id = Column(String(64), nullable=True)
    dhan_entry_order_id = Column(String(64), nullable=True)
    dhan_exit_order_id = Column(String(64), nullable=True)

    exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    realized_pnl_pct = Column(Float, nullable=True)

    capital_risked = Column(Float, nullable=False)
    is_first_live_order = Column(Boolean, nullable=False, default=False)

    opened_at = Column(DateTime, nullable=False, default=_now)
    closed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class ScalpCandidateLog(Base):
    """Audit trail of every scanned candidate and why it was taken or
    skipped — mirrors the diagnostic value of real-trade-service's own
    WAIT-reason logging in candidate_engine/candidates.py."""
    __tablename__ = "scalp_candidate_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    window_source = Column(String(8), nullable=False)
    pct_change = Column(Float, nullable=False)
    composite_score = Column(Float, nullable=True)
    decision = Column(String(16), nullable=False)  # "ENTERED" | "SKIPPED"
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now, index=True)


class ScalpGateState(Base):
    """Single-row-per-mode operational state: armed/disarmed, EOD sweep
    fired-today guard, daily-loss kill-switch tripped flag. Mirrors
    real-trade-service's own gate-state-machine pattern (main.py's
    _check_and_expire_gates) at a much smaller scale."""
    __tablename__ = "scalp_gate_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True, default="REAL")
    is_armed = Column(Boolean, nullable=False, default=False)
    armed_at = Column(DateTime, nullable=True)
    eod_squareoff_fired_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    daily_loss_kill_switch_tripped = Column(Boolean, nullable=False, default=False)
    daily_loss_kill_switch_tripped_date = Column(String(10), nullable=True)
    orders_placed_today = Column(Integer, nullable=False, default=0)
    orders_placed_today_date = Column(String(10), nullable=True)
    first_live_order_done = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
