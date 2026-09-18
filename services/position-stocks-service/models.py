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
    Index,
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
    # BUG FIX (2026-09-13, session 11): daily_loss_kill_switch_tripped /
    # daily_loss_kill_switch_tripped_date were added to db.py's
    # _COLUMN_MIGRATIONS (so the DB column exists) and are read/written
    # throughout capital/ledger.py (reserve_capital, release_capital,
    # reset_daily, get_state) — but were NEVER actually declared as Column
    # attributes on THIS class. A DB-level ALTER TABLE does nothing for a
    # SQLAlchemy ORM instance's Python attributes; those come from the
    # mapped class definition, not table reflection. So every
    # row.daily_loss_kill_switch_tripped access kept raising AttributeError
    # (-> 500 on GET /ledger) even after the DB column existed and even
    # across a fresh redeploy — the previous fix only did half the job.
    # Adding them here (matching db.py's oracle/pg DDL types: NUMBER(1)/
    # BOOLEAN <-> Boolean, VARCHAR2(10)/VARCHAR(10) <-> String(10)) is
    # what actually resolves it.
    daily_loss_kill_switch_tripped = Column(Boolean, nullable=False, default=False)
    daily_loss_kill_switch_tripped_date = Column(String(10), nullable=True)
    # BUG FIX (session13 audit): reset_daily() existed in capital/ledger.py
    # but was never called anywhere in the service — no scheduler, no
    # startup hook — so once the daily-loss kill switch tripped it stayed
    # tripped forever, and realized_pnl_today accumulated across days
    # instead of resetting, silently corrupting the loss-limit math for
    # every day after the first trip. Fixed with a lazy reset-on-date-
    # change check (this field tracks the IST date realized_pnl_today/
    # kill-switch were last valid for) run on every ledger read/write
    # instead of a scheduler, so it self-heals even if the service was
    # down across midnight.
    pnl_last_reset_date = Column(String(10), nullable=True)
    # BUG FIX (Issue #2): position-stocks' daily-loss kill switch previously
    # only watched its OWN realized_pnl_today, while real-trade-service's
    # losses on the SAME shared Dhan account were invisible to it. A -₹1,436
    # loss on real-trade's side still left position-stocks freely entering new
    # positions. This column caches the last-known realized_pnl_today fetched
    # from real-trade-service's /status/REAL (synced inside sync_from_broker()
    # at the same cadence, not on the hot entry path). reserve_capital() now
    # adds this to its own pnl_today before evaluating the kill-switch
    # threshold — so cross-service losses on the same account are accounted for.
    peer_realized_pnl_today = Column(Float, nullable=False, default=0.0)
    peer_pnl_last_synced_at = Column(DateTime, nullable=True)
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
    window_source = Column(String(8), nullable=False)  # "1m" | "5m" | "15m" | "60m"  (AUDIT FIX: "1m" window added 2026-09-12 was missing from comment)
    status = Column(String(24), nullable=False, default="OPEN", index=True)
    # OPEN, TARGET_HIT, STOP_HIT, EOD_SQUAREOFF, MANUAL_EXIT, ERROR,
    # EXIT_LEGS_REJECTED (super-order exit legs were rejected by Dhan —
    # circuit-limit / surveillance; awaiting EOD squareoff plain MARKET SELL),
    # STAGNATION_EXIT (this session: promoted from a MANUAL_EXIT-labeled
    # cosmetic reason to its own real terminal status — see
    # orders/eod_squareoff.py::close_position_now/run_stagnation_exit and
    # orders/reconcile.py's _FLAT_SELL_PENDING_STATUSES)

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

    # SESSION 33 AUDIT FIX: GET /positions and GET /trades/history (main.py)
    # both order by opened_at DESC on every dashboard poll — had no
    # supporting index. See db.py's _ensure_hot_path_indexes for the
    # additive migration this table needs on an already-deployed DB
    # (create_all() only adds an index to a table it's also creating for
    # the first time).
    __table_args__ = (Index("ix_scalp_positions_opened_at", "opened_at"),)


class ScalpCandidateLog(Base):
    """Audit trail of every scanned candidate and why it was taken or
    skipped — mirrors the diagnostic value of real-trade-service's own
    WAIT-reason logging in candidate_engine/candidates.py.

    fundamental_score/technical_score/market_cap_cr/has_positive_catalyst
    (added session 6) come from screening/quality_gate.py's best-effort
    check, run only on the top few candidates before entry — see that
    module's docstring. All nullable: the quality gate is lenient by design,
    so a None here means "data unavailable that cycle", not "checked and
    bad"."""
    __tablename__ = "scalp_candidate_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    window_source = Column(String(8), nullable=False)
    pct_change = Column(Float, nullable=False)
    composite_score = Column(Float, nullable=True)
    decision = Column(String(16), nullable=False)  # "ENTERED" | "SKIPPED"
    reason = Column(Text, nullable=True)
    fundamental_score = Column(Float, nullable=True)
    technical_score = Column(Float, nullable=True)
    market_cap_cr = Column(Float, nullable=True)
    has_positive_catalyst = Column(Boolean, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now, index=True)


class ScalpGateState(Base):
    """Single-row-per-mode operational state: armed/disarmed, EOD sweep
    fired-today guard, daily-loss kill-switch tripped flag. Mirrors
    real-trade-service's own gate-state-machine pattern (main.py's
    _check_and_expire_gates) at a much smaller scale.

    service_enabled (added 2026-09-12, session 4) is a coarser, separate switch
    from is_armed: is_armed only gates whether real orders can be PLACED;
    service_enabled gates the whole module — screening AND entries — and
    is checked independently in main.py's trading loop, so you can pause
    Position Stocks entirely (maintenance, ruling it out while debugging
    something else) without losing/re-setting the is_armed flag. Defaults
    to True so existing/fresh rows behave exactly as before this field
    existed. Exit reconciliation and the EOD square-off sweep intentionally
    ignore this flag (and is_armed) — open real-money positions must never
    be left unmanaged just because the module is toggled off (tracking doc
    §3.7: "no exceptions").

    auto_pilot_enabled (added session 6) is a third, finer switch, mirroring
    real-trade-service's is_armed/auto_pilot_enabled split: screening still
    runs (so /candidates stays live) whenever armed+service_enabled+market
    open, regardless of this flag — only the AUTOMATIC entry attempt each
    cycle is gated by it. Turning auto-pilot off lets you watch what the
    screener would do without it acting on anything. The manual
    `POST /cycle/run` endpoint deliberately bypasses this flag (but still
    requires is_armed) — same as real-trade-service's manual cycle trigger
    working regardless of auto-pilot state. Defaults to True so behavior is
    unchanged for anyone who hasn't touched it yet.

    NOTE ON SCHEMA CHANGES: SQLAlchemy's create_all() only creates missing
    TABLES, never ALTERs existing ones. As of session 5 this is no longer a
    manual step — db.py's init_tables() runs _ensure_columns() right after
    create_all(), an idempotent, dialect-aware migration that adds any
    column here missing from an already-deployed table automatically on
    boot. Add a new column here, then add one tuple to db.py's
    _COLUMN_MIGRATIONS — no manual ALTER TABLE needed.
    """
    __tablename__ = "scalp_gate_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True, default="REAL")
    is_armed = Column(Boolean, nullable=False, default=False)
    armed_at = Column(DateTime, nullable=True)
    service_enabled = Column(Boolean, nullable=False, default=True)
    auto_pilot_enabled = Column(Boolean, nullable=False, default=True)
    last_cycle_run_at = Column(DateTime, nullable=True)
    last_cycle_run_trigger = Column(String(16), nullable=True)  # "AUTO" | "MANUAL"
    eod_squareoff_fired_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    daily_loss_kill_switch_tripped = Column(Boolean, nullable=False, default=False)
    daily_loss_kill_switch_tripped_date = Column(String(10), nullable=True)
    orders_placed_today = Column(Integer, nullable=False, default=0)
    orders_placed_today_date = Column(String(10), nullable=True)
    first_live_order_done = Column(Boolean, nullable=False, default=False)
    # session69: runtime (DB-backed, no restart needed) on/off switch for
    # orders/eod_squareoff.py::run_stagnation_exit — same pattern as
    # auto_pilot_enabled/service_enabled above, toggled from the frontend
    # via POST /stagnation-exit/enable|disable instead of an env var that'd
    # need a redeploy. STAGNATION_EXIT_MINUTES/_BAND_PCT stay config.py-only
    # tuning knobs (not exposed as a toggle — same as MAX_ENTRY_RANGE_POSITION
    # etc.). Defaults False — unchanged behavior until explicitly turned on.
    stagnation_exit_enabled = Column(Boolean, nullable=False, default=False)
    # this session: tracks the last IST calendar date the trade-history
    # retention cleanup ran, same date-tracking pattern as
    # eod_squareoff_fired_date above — lets the fast-reconcile loop run it
    # at most once per day without a separate scheduler process.
    retention_cleanup_last_run_date = Column(String(10), nullable=True)  # 'YYYY-MM-DD'
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SharedOrderBudget(Base):
    """Cross-service Dhan account-wide order-rate counter (tracking doc §3.8).

    NOTE ON A PRE-EXISTING GAP FOUND SESSION 7: TRACKING.md/STATUS.md have
    described this feature as fully built since an earlier session, but
    until session 7 no such file, table, or wiring actually existed
    anywhere in the repo — documentation had drifted ahead of the code.
    This is the real implementation.

    Dhan's account-wide order cap (~5,000-7,000 orders/day) is shared
    between real-trade-service and this service (same Dhan account) —
    neither service's own per-service budget (this service's
    `DAILY_ORDER_BUDGET`, real-trade-service's own limits) knows about the
    other's order volume. This table is the one thing both already
    unconditionally share: the same physical Postgres/Oracle DB. Built as a
    DB-backed counter rather than Redis because this codebase's Redis layer
    is Upstash-based and optional/off-by-default — a real-money order-rate
    guard shouldn't depend on optional infrastructure.

    One row per calendar day (IST). `capital/shared_order_budget.py` (this
    service) and `execution/shared_order_budget.py` (real-trade-service,
    duplicated logic, not imported — same isolation rationale as everywhere
    else in this file) both read/increment the same row. Soft governor, not
    a financial ledger: one read + one upsert per order attempt, fails OPEN
    on any DB error — a broken rate-governor must never itself block a real
    exit. Table name deliberately NOT prefixed `scalp_` (unlike every other
    table in this file) since it's explicitly meant to be shared, not
    scoped to this service."""
    __tablename__ = "stockky_shared_order_budget"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True)  # 'YYYY-MM-DD' IST
    orders_placed_today = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SharedSymbolLock(Base):
    """Cross-service claim on a symbol currently held at the broker.

    THE BUG THIS FIXES (session60): both this service and real-trade-service
    trade through the SAME Dhan account. Dhan holds one consolidated
    position per symbol at the broker — it has no concept of "these N
    shares are position-stocks-service's" vs "these are real-trade-service's".
    Nothing was stopping both services from independently BUYing the same
    symbol (confirmed: AEGISVOPAK was bought by both services on the same
    day). Once that happens, each service's own local row (qty, entry
    price, stop/target) points at a share of one real, merged broker
    position, and a later SELL sized/priced from only one side's local
    record can mismatch what Dhan's own order book reports for that
    symbol — exactly the "Broker order-type mismatch" Telegram alert this
    was diagnosed from.

    One row per symbol CURRENTLY held by either service (not one row per
    day, unlike SharedOrderBudget above) — deleted once the holding side
    reports the position fully flat. `capital/shared_symbol_lock.py` (this
    service) and `execution/shared_symbol_lock.py` (real-trade-service,
    duplicated logic, not imported — same isolation rationale as
    SharedOrderBudget) both check/claim/release the same row. FAIL-OPEN
    on any DB error, same as SharedOrderBudget: a broken lock must never
    itself block a real entry or a real exit — worst case on failure is a
    reversion to today's actual (buggy) behavior, not a new way to get
    stuck. Table name deliberately NOT prefixed `scalp_`, for the same
    reason as stockky_shared_order_budget: it is explicitly shared, not
    scoped to this service."""
    __tablename__ = "stockky_shared_symbol_lock"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, unique=True, index=True)
    held_by_service = Column(String(32), nullable=False)  # "position-stocks-service" | "real-trade-service"
    held_by_mode = Column(String(8), nullable=True)  # real-trade-service's REAL/DEMO; null for this service (REAL-only)
    claimed_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class ScalpIntradayRestrictedSecurity(Base):
    """Learned list of NSE securities Dhan has rejected as 'not allowed to
    be traded in Intraday' (T2T / ASM / GSM surveillance stocks).

    DESIGN: mirrors real-trade-service's IntradayRestrictedSecurity
    (trade_intraday_restricted) exactly, but lives in a separate table
    (scalp_intraday_restricted) so this service's init_tables() can create
    and own it without ever touching a table real-trade-service manages.

    Built from real rejections only — there is no static eligibility flag
    in Dhan's scrip master, so this list is seeded entirely from live SELL
    rejections this service observes in orders/eod_squareoff.py and
    orders/entry.py, then consulted in _run_cycle() before a new BUY so
    known-restricted symbols are filtered from candidates before capital
    or a Dhan call is committed to them.
    """
    __tablename__ = "scalp_intraday_restricted"

    symbol = Column(String(32), primary_key=True)
    first_detected_at = Column(DateTime, nullable=False, default=_now)
    last_detected_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    hit_count = Column(Integer, nullable=False, default=1)
    last_detail = Column(String(255), nullable=True)


class ScalpQualityCache(Base):
    """Last-known-good quality-gate scores per symbol (session68).

    WHY THIS EXISTS: screening/quality_gate.py is fail-open by design — a
    timeout or non-200 from analysis-intelligence-service leaves a field
    None, treated as "unknown = pass". Confirmed root cause of MANGALAM /
    GEEKAYWIRE / JISLJALEQS (all ₹30-32, thin liquidity) getting entered
    on 2026-09-17 despite the MIN_MARKET_CAP_CR=500cr floor: the fetch
    for those symbols almost certainly timed out and the gate waved them
    through blind. This table lets a live timeout fall back to the last
    value this service actually fetched successfully for that symbol,
    instead of blind None — see quality_gate.get_cache_batch/
    upsert_cache_batch and check()'s `cached` param. A symbol with no
    prior successful fetch still fails open the first time, same as
    before — this only removes the blind spot for repeat offenders,
    which MANGALAM/GEEKAYWIRE/JISLJALEQS-class names (scanned every
    cycle, same low-liquidity names showing up repeatedly) are.
    """
    __tablename__ = "scalp_quality_cache"

    symbol = Column(String(32), primary_key=True)
    fundamental_score = Column(Float, nullable=True)
    technical_score = Column(Float, nullable=True)
    market_cap_cr = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
