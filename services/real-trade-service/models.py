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

    # 2026-09-10 (session22, user request): fourth scheduled feature, same
    # idiom as the three above — end-of-day re-scan that queues an
    # "overnight priority" list for tomorrow's pre-pick instead of placing
    # any order today. See config.py's EOD_SIGNAL_SCAN_* block and
    # auto_pilot.py's _eod_signal_scan.
    eod_signal_scan_enabled = Column(Boolean, nullable=False, default=False)
    eod_signal_scan_enabled_at = Column(DateTime, nullable=True)
    eod_signal_scan_last_run = Column(String(10), nullable=True)

    # 2026-09-18 fix (user report: "no new toggle shows" for selective
    # overnight holding). execution/auto_pilot.py's _select_overnight_holds
    # (2026-09-18) already ships live, gated only by config.py's
    # OVERNIGHT_HOLD_ENABLED env var — no dashboard control existed, and
    # "EOD square-off" above still described itself as flattening every
    # position, which is no longer true whenever this is on. Unlike the
    # four toggles above (all default OFF, opt-in), this one defaults ON
    # to match the env var's existing default ("true") and the fact that
    # this behavior is already live in REAL — flipping the dashboard
    # switch off is what actually changes anything for an existing
    # deployment. See execution/auto_pilot.py's _overnight_hold_enabled.
    overnight_hold_enabled = Column(Boolean, nullable=False, default=True)
    overnight_hold_enabled_at = Column(DateTime, nullable=True)

    # 2026-09-17 (session56, user request): fifth scheduled feature —
    # after-hours news scan. Polls Moneycontrol/LiveMint/ET RSS feeds hourly
    # between market close and next pre-open, classifies headlines, scores
    # symbols, and upserts into NextDayWatchlistEntry so _prepick can pull
    # them at 09:00 as pre-seeded candidates.
    # Unlike the four above, this feature runs OUTSIDE market hours
    # (15:45–08:45 IST window), so there is no _last_run date guard — the
    # scan runs on every AFTERHOURS_SCAN_INTERVAL_SECONDS tick while the
    # window is active. A final "finalize" pass runs at ~08:45 to lock the
    # top-N ranking before the open. The feature is DEFAULT OFF; the DB
    # toggle is the sole authority (same pattern as the four above).
    afterhours_news_scan_enabled = Column(Boolean, nullable=False, default=False)
    afterhours_news_scan_enabled_at = Column(DateTime, nullable=True)
    # 2026-09-17 fix (session56 audit): the finalize pass's once-per-day guard
    # was originally an in-memory module dict in auto_pilot.py, which broke
    # the "each feature fires at most once per IST trading day, tracked by a
    # persisted _last_run column" contract this table otherwise guarantees —
    # a restart between ~08:45 and market open could re-fire the finalize
    # notification. Persisted here so it survives restarts like every other
    # scheduled feature's guard.
    afterhours_finalize_last_run = Column(String(10), nullable=True)  # "YYYY-MM-DD"

    # 2026-09-17 (session58, user request): manual-trigger verification.
    # Set after EVERY tick of the after-hours scan — scheduled (the
    # background loop tick) or manual (the new POST /afterhours/run-manual
    # button) — so the frontend can show a single "last run" indicator that
    # is accurate regardless of which path fired it. _ok is None until the
    # very first run; True/False afterward (False on any exception raised
    # by that tick, mirroring the scan/finalize try/except in
    # execution/auto_pilot.py's _afterhours_scan_body).
    afterhours_scan_last_run_at = Column(DateTime, nullable=True)
    afterhours_scan_last_run_ok = Column(Boolean, nullable=True)

    # 2026-09-19 (audit finding): sixth scheduled feature — pre-market CDSL
    # eDIS verification check. Overnight-held positions (see
    # overnight_hold_enabled above) become real CNC holdings that need
    # manual TPIN verification in the Dhan app before they can be sold the
    # next day — see execution/auto_pilot.py's _edis_morning_check and
    # config.py's EDIS_MORNING_CHECK_ENABLED comment for the full "why".
    # Defaults ON (same reasoning as overnight_hold_enabled above: this is
    # a safety check for behavior that's already live, not new exposure).
    edis_morning_check_enabled = Column(Boolean, nullable=False, default=True)
    edis_morning_check_enabled_at = Column(DateTime, nullable=True)
    edis_check_last_run = Column(String(10), nullable=True)  # "YYYY-MM-DD"

    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ── Accounts (one row per mode: DEMO paper account, REAL linked Dhan account) ─
class TradeAccount(Base):
    __tablename__ = "trade_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False, unique=True)  # "DEMO" | "REAL"
    starting_capital = Column(Float, nullable=False, default=100000.0)
    current_equity = Column(Float, nullable=False, default=100000.0)
    cash_available = Column(Float, nullable=False, default=100000.0)
    # ADDED (session52, capital-split fix): cash_available above is now
    # THIS service's capped share (config.CAPITAL_SHARE_PCT, default 50%)
    # of Dhan's real available balance — see execution/equity_sync.py.
    # broker_cash_available keeps the RAW, uncapped figure Dhan actually
    # reported, so risk_engine's new "capital_share_cap" check can compute
    # the shared account's true total (broker_cash_available + this
    # service's own open-position market value) without reverse-deriving
    # it from an already-halved number.
    broker_cash_available = Column(Float, nullable=False, default=0.0)
    realized_pnl_today = Column(Float, nullable=False, default=0.0)
    realized_pnl_total = Column(Float, nullable=False, default=0.0)
    # ADDED (this session): realized_pnl_today was NEVER actually reset on a
    # new trading day anywhere in this service — despite main.py's /status
    # comment claiming it does, and despite risk_engine's daily_loss_limit
    # check (engine.py) and every BUY-gating AccountState (entry_engine.py,
    # manual_engine.py) treating it as "today's" P&L. In reality it only ever
    # accumulated (see portfolio.py's close_position/record_real_exit_fill,
    # which increment both realized_pnl_today and realized_pnl_total by the
    # same amount, forever) — i.e. it silently became a second all-time
    # total, identical in behavior to realized_pnl_total. This tracks the IST
    # date realized_pnl_today is currently valid for; portfolio.get_account()
    # lazily zeroes realized_pnl_today (only — realized_pnl_total is a true
    # all-time figure and is untouched) whenever that date has passed, the
    # same lazy-reset-on-read idiom position-stocks-service's capital/
    # ledger.py already uses for the same reason (self-heals even if the
    # service was down across midnight; no scheduler needed).
    pnl_last_reset_date = Column(String(10), nullable=True)
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
    # 2026-09-18 fix (follow-on item #5 from the cost-model audit): these two
    # cost-gate knobs (Gate 5.6, cost_model.evaluate_entry_cost_gate) were
    # env-var-only (config.MIN_TRADE_VALUE / MIN_EDGE_TO_COST_RATIO) while
    # every other risk knob on this row is admin-editable per-mode with a
    # confirm step. NULL means "use the config.py/env-var default for this
    # mode" — entry.py's cost-gate call resolves DB override first, config.py
    # fallback second, so an already-deployed row with these NULL changes
    # nothing until an admin explicitly sets them.
    min_trade_value = Column(Float, nullable=True)
    min_edge_to_cost_ratio = Column(Float, nullable=True)
    # 2026-09-21 fix (session79): flat rupee ceiling on a single trade's
    # position value — see config.MAX_TRADE_VALUE and risk_engine/engine.py
    # §5c-ii docstrings. Same NULL-means-"use config.py default" idiom as
    # min_trade_value/min_edge_to_cost_ratio above.
    max_trade_value = Column(Float, nullable=True)
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
    # 2026-09-02 Short-Term Trading Upgrade: set when this candidate was
    # produced by watchlist_engine's entry-trigger pass — lets exit_engine
    # trace back to the catalyst's horizon_class for catalyst-aware exits.
    # NULL for all other source tracks — no behavior change.
    watchlist_entry_id = Column(Integer, ForeignKey("trade_watchlist.id"), nullable=True)
    # 2026-09-10 (session22, user request): set True only for candidates
    # injected by auto_pilot._prepick from the previous evening's EOD signal
    # scan snapshot (see resilience/local_cache + config.EOD_SIGNAL_SCAN_*).
    # False for every normal candidate. entry_engine reads this to apply
    # config.ENTRY_OVERNIGHT_PRIORITY_BONUS to Gate 6's ranking only — it
    # changes nothing about gates 1-5 or risk_engine.
    overnight_priority = Column(Boolean, nullable=False, default=False)
    # 2026-09-11 (session23, user request): set by auto_pilot._prepick via
    # market_context.sector_signal when config.US_SECTOR_SIGNAL_ENABLED is
    # on. A small, capped +/-config.US_SECTOR_BONUS_CAP points, derived from
    # how this candidate's NSE sector's closest US sector ETF closed
    # overnight. 0.0 (the default) for every candidate when the feature is
    # off, unmapped, or data is unavailable — never a gate, only a Gate 6
    # ranking nudge in entry_engine/entry.py, same posture as
    # overnight_priority above.
    us_sector_bonus = Column(Float, nullable=False, default=0.0)

    # SESSION 33 AUDIT FIX: entry_engine.evaluate_mode's hot-path query
    # (filter_by(mode=mode, consumed=False).order_by(received_at.asc())),
    # run every auto-pilot tick forever, had no supporting index — see
    # db.py's _ensure_hot_path_indexes for the additive migration this
    # table needs on an already-deployed DB (create_all() only adds an
    # index to a table it's also creating for the first time).
    __table_args__ = (
        Index("ix_trade_candidates_mode_symbol", "mode", "symbol"),
        Index("ix_trade_candidates_mode_consumed_recv", "mode", "consumed", "received_at"),
    )


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
    # 2026-09-18 fix (follow-on item #6): entry_engine's Gate 5.6 (cost-model
    # floor) writes "cost_model" here on every WAIT it produces, so the
    # dashboard can badge/filter these distinctly from an ordinary risk-engine
    # WAIT without brittle string-matching on `reasoning`. NULL for every
    # other gate's decisions (unaffected, no behavior change).
    gate_tag = Column(String(32), nullable=True)
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
    # 2026-09-15 fix (session38 — DATAMATICS "insufficient funds" SELL
    # rejections): the actual product_type ("CNC" | "INTRADAY"/"MIS") sent
    # to Dhan for THIS order. Never persisted before this fix — entry.py
    # (always CNC, implicit via dhan_client.place_order's default — never
    # passed explicitly) and manual_engine.py (explicit, user-chosen) both
    # decided a product_type at placement time but threw it away once the
    # order was sent. exit_engine._send_real_sell had no way to know what
    # the opening BUY actually used, so it guessed from opened_at's
    # same-day-ness instead — wrong whenever a same-day position was
    # actually bought CNC (the automated entry path's only mode: see
    # entry_engine/entry.py, which never passes product_type and so always
    # gets dhan_client.place_order's CNC default). A same-day CNC BUY has
    # no matching MIS position at Dhan, so an INTRADAY SELL against it
    # prices as a fresh naked short and gets margin-rejected. Set at BUY
    # placement time (entry.py/manual_engine.py); NULL for pre-fix orders
    # and for SELL orders (which don't need it — see TradePosition.
    # entry_product_type, which is what exit.py actually reads).
    product_type = Column(String(16), nullable=True)
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
    # session110 fix (partial fills booked at the cumulative average price):
    # Dhan's averageTradedPrice on a PART_TRADED/TRADED order is the CUMULATIVE
    # average of the whole order, but reconcile books each NEW increment
    # (delta_qty). Booking every increment at that cumulative average drifts
    # the position's average entry price, the cash debit and (for SELLs) the
    # realized P&L. This column holds the broker's cumulative filled VALUE
    # (filledQty x averageTradedPrice) as of the last poll reconcile booked, so
    # the next poll can derive the increment's own price:
    #     (notional_now - broker_fill_notional) / delta_qty
    # NULL on every order booked before this column existed and on orders with
    # no fill yet — reconcile then books that one increment at the cumulative
    # average exactly as before and starts tracking from there. See
    # execution/reconcile.py::_increment_price and db.py::
    # _ensure_fill_notional_column.
    broker_fill_notional = Column(Float, nullable=True)
    # 2026-09-02 Short-Term Trading Upgrade: copied from the originating
    # TradeCandidate.watchlist_entry_id at order-creation time (entry_engine),
    # so portfolio.py's fill handlers can stamp the resulting TradePosition
    # without an extra join. NULL for manual/non-watchlist orders.
    watchlist_entry_id = Column(Integer, ForeignKey("trade_watchlist.id"), nullable=True)
    # 2026-09-12 fix (audit finding — volume_shock time_stop): copied from
    # the originating TradeCandidate.source_tab at order-creation time
    # (entry_engine, same spot watchlist_entry_id is copied), so
    # portfolio.py's fill handlers can stamp the resulting TradePosition
    # without an extra join back through TradeDecision -> TradeCandidate.
    # NULL for manual orders (manual_engine never sets this).
    source_tab = Column(String(32), nullable=True)

    # 2026-09-18 fix (selective overnight hold — see execution/auto_pilot.py's
    # _select_overnight_holds and config.py's OVERNIGHT_HOLD_* block): copied
    # from the originating TradeCandidate.decision_label / conviction_score at
    # order-creation time (same pattern as source_tab above), so portfolio.py's
    # fill handlers can stamp the resulting TradePosition without a join back
    # through TradeDecision -> TradeCandidate. NULL for manual orders (no
    # originating candidate).
    entry_decision_label = Column(String(32), nullable=True)
    entry_conviction_score = Column(Float, nullable=True)

    # 2026-09-18 audit fix #2 (regime-override win-rate tracking): True only
    # for a BUY placed through the market-regime gate's ENTRY_REGIME_OVERRIDE_
    # TOP_N bypass (config.py) — the single highest-conviction candidate let
    # through at ENTRY_REGIME_OVERRIDE_RISK_SCALE sizing while the regime
    # gate is otherwise blocking every entry. Set at order-creation time in
    # entry_engine/entry.py from that same cycle's is_regime_override flag.
    # False (default) for every normal entry and every pre-migration row.
    # Threaded onto TradePosition.is_regime_override at fill time (see that
    # column's docstring) so GET /stats/regime-override can report the
    # win-rate of these deliberately-against-the-regime-read entries
    # without a join back through TradeDecision.
    is_regime_override = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    # SESSION 33 AUDIT FIX: GET /orders/{mode} (main.py's list_orders) filters
    # by mode + a created_at cutoff and orders by created_at DESC on every
    # Orders tab poll — had no supporting index. See db.py's
    # _ensure_hot_path_indexes for the additive migration this table needs
    # on an already-deployed DB.
    __table_args__ = (Index("ix_trade_orders_mode_created", "mode", "created_at"),)


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
    # 2026-09-02 Short-Term Trading Upgrade: set (from TradeOrder.watchlist_entry_id)
    # when position was opened off a watchlist-sourced entry. exit_engine._load_profile
    # uses it to look up horizon_class for catalyst-aware exit profile.
    # NULL for manual trades and pre-migration positions — fall back to global defaults.
    watchlist_entry_id = Column(Integer, ForeignKey("trade_watchlist.id"), nullable=True)

    # 2026-09-09 fix (insufficient-funds SELL rejections — see
    # portfolio.import_broker_holdings and exit_engine.exit._send_real_sell):
    # True only for a position this system never bought itself — a
    # pre-existing Dhan demat holding pulled in by import_broker_holdings.
    # opened_at on those rows is set to the IMPORT moment (the real purchase
    # date isn't available from Dhan's holdings API), which made exit.py's
    # "was this opened today?" check misread a long-held holding as a
    # same-day round trip and sell it product_type="INTRADAY" — a product
    # type Dhan has no matching MIS position to net against, so it was
    # priced like a fresh naked short and margin-rejected ("insufficient
    # funds"). broker_imported lets exit.py force CNC for these regardless
    # of opened_at, independent of the same-day heuristic. False (default)
    # for every position this system opened itself via entry_engine/
    # manual_engine, where opened_at is trustworthy and the existing
    # same-day/CNC logic is correct as-is.
    broker_imported = Column(Boolean, nullable=False, default=False)

    # 2026-09-15 fix (session38): copied from the opening BUY TradeOrder's
    # new `product_type` column (portfolio.record_real_fill /
    # try_fill_entry) at position-open time. Lets exit_engine._send_real_sell
    # mirror what was ACTUALLY bought instead of guessing from opened_at's
    # same-day-ness — see TradeOrder.product_type's docstring for the full
    # incident. NULL for every position opened before this migration (and
    # for broker_imported holdings, which don't go through a Stockky BUY at
    # all) — exit.py falls back to the pre-fix same-day heuristic only when
    # this is NULL, so no behavior changes for positions already open when
    # this ships.
    entry_product_type = Column(String(16), nullable=True)

    # 2026-09-12 fix (audit finding — volume_shock candidates were getting
    # the 10-day global time-stop instead of the intended EOD+1 exit):
    # copied from TradeOrder.source_tab (itself copied from the originating
    # TradeCandidate.source_tab) at position-open time in portfolio.py's
    # try_fill_entry / record_real_fill. watchlist_entry_id is NULL for
    # volume_shock candidates (they never go through the watchlist engine —
    # see candidate_engine._refresh_volume_shock_candidates), so
    # exit_engine._load_profile could not tell a volume_shock position apart
    # from a plain manual trade and fell through to the 10-day/6-day-warn
    # global defaults. This column lets _load_profile route
    # source_tab="volume_shock" positions to the "short" horizon exit
    # profile (5-day hold) even with no watchlist_entry_id. NULL for manual
    # trades, broker-imported holdings, and pre-migration rows — all of
    # which keep falling back to the existing global-default behavior.
    source_tab = Column(String(32), nullable=True)

    # 2026-09-18 fix (selective overnight hold — see execution/auto_pilot.py's
    # _select_overnight_holds and config.py's OVERNIGHT_HOLD_* block): copied
    # from TradeOrder.entry_decision_label / entry_conviction_score at fill
    # time (portfolio.py's try_fill_entry / record_real_fill, same spot
    # source_tab is copied), so EOD square-off can decide whether this
    # specific position is eligible to skip flattening without a join back
    # through TradeOrder -> TradeDecision -> TradeCandidate. NULL for manual
    # trades, broker-imported holdings, and pre-migration rows — all of which
    # are simply never overnight-hold eligible (fail-safe: they square off
    # exactly as before).
    entry_decision_label = Column(String(32), nullable=True)
    entry_conviction_score = Column(Float, nullable=True)

    # 2026-09-18 fix (follow-on item #2 from the cost-model audit): cost_model.py
    # only ever gated ENTRIES — nothing recorded what a position's round-trip
    # actually cost after the fact, so realized_pnl was a pre-cost (gross)
    # number with no net-of-cost figure anywhere. portfolio.py's close_position
    # (DEMO) / record_real_exit_fill (REAL) now accumulate both of these on
    # every full or partial close, using the same estimate_round_trip_cost()
    # Gate 5.6 already uses at entry. NULL/0 on pre-migration rows and any row
    # that hasn't closed yet — best-effort estimate, never used to gate
    # anything, so a failure here never blocks or corrupts the authoritative
    # gross realized_pnl above.
    net_realized_pnl = Column(Float, nullable=True)
    realized_cost_estimate = Column(Float, nullable=True)

    # 2026-09-18 fix (follow-on item #6 from the cost-model audit): the reason
    # execution/auto_pilot.py._select_overnight_holds decided to keep this
    # specific position open past EOD square-off used to only ever be logged
    # (logger.info) — never persisted anywhere queryable, so the dashboard had
    # no way to show *why* a position skipped square-off. Set once, the cycle
    # it's decided; left NULL for every position that squared off normally or
    # was never a hold candidate.
    overnight_hold_reason = Column(Text, nullable=True)

    # 2026-09-15 fix (session40 — DATAMATICS position 81, 89 consecutive
    # REJECTED zero-fill exit-SELL attempts over ~4.5h with no backoff and
    # no operator alert — see exit_engine/exit.py._send_real_sell's
    # cooldown gate and execution/reconcile.py's dead-SELL handling for
    # where these are written). Tracks consecutive broker-rejected,
    # zero-fill SELL attempts for THIS position so repeated rejections
    # trigger an escalating cooldown (instead of retrying every single
    # cycle forever) and a one-time CRITICAL alert once a threshold is
    # crossed. Reset to 0 / NULL the moment any fill (full or partial) is
    # booked for this position's SELL. NULL/0 for every pre-migration row
    # and every position that has never had a SELL rejected — no behavior
    # change for the normal case.
    consecutive_exit_failures = Column(Integer, nullable=False, default=0)
    last_exit_failure_at = Column(DateTime, nullable=True)

    # 2026-09-18 audit fix #2 (regime-override win-rate tracking — see
    # models.py TradeOrder.is_regime_override docstring for the full
    # rationale). Copied from the opening BUY TradeOrder's is_regime_override
    # at fill time (portfolio.py's try_fill_entry / record_real_fill, same
    # spot entry_decision_label is copied). False (default) for every
    # position not opened via the regime-override bypass and every
    # pre-migration row — GET /stats/regime-override only ever counts rows
    # where this is True.
    is_regime_override = Column(Boolean, nullable=False, default=False)

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


# 2026-09-11 addition: generalizes the single-metric pattern above (which
# only ever tracked market_score) to any measured quantity, for
# adaptive_market_params.py. One row per (metric_name, recorded_at) reading
# — e.g. "universe_atr_pct" (this cycle's average pre-shock ATR% across the
# volume-shock candidate batch). Same rolling-percentile-with-warm-up-
# fallback contract as MarketRegimeHistory/adaptive_thresholds.py.
class AdaptiveMetricSnapshot(Base):
    __tablename__ = "trade_adaptive_metric_history"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    metric_name = Column(String(64), nullable=False)
    value       = Column(Float, nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        Index("ix_adaptive_metric_history_name_recorded", "metric_name", "recorded_at"),
    )


# ── Short-Term Trading Upgrade (2026-09-02) ─────────────────────────────────
# Watchlist: catalyst detection (Stage 1) is now separate from entry timing
# (Stage 2). watchlist_engine.watchlist writes one row per detected catalyst
# here; entry_engine.evaluate_watchlist_entries is the Stage-2 trigger pass
# that reads active rows, checks price hasn't run past entry_band_pct from
# catalyst_price, and — if still within band — inserts a tagged TradeCandidate
# into the existing pipeline so exit_engine can later apply a catalyst-aware
# exit profile.
class WatchlistEntry(Base):
    __tablename__ = "trade_watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)   # "DEMO" | "REAL"
    symbol = Column(String(32), nullable=False, index=True)

    catalyst_type = Column(String(16), nullable=False)    # "bulk_block"|"insider"|"results"|"board"|"ipo"|"volume_shock"
    catalyst_price = Column(Float, nullable=False, default=0.0)  # 0.0 = unknown (Tier 3 rows set on first sight)
    # AUDIT FIX (this session): a diagnostic gap found while investigating a
    # run of "missed" board/bulk_block/results catalysts that overran their
    # entry_band_pct by a moderate, consistent margin even though the
    # per-cycle trigger pass (entry_engine.evaluate_watchlist_entries) runs
    # every AUTO_PILOT_INTERVAL_SECONDS (~3 min) — too fast to explain a
    # multi-percent overrun by our own polling latency alone. The likely
    # cause: watchlist_engine/sources.py's Tier 1 normalizer sets
    # catalyst_price = item.get("price") or item.get("close") — for any hot-
    # pick item whose live "price" field is absent, catalyst_price silently
    # becomes the PREVIOUS trading day's close instead of a live tick. Any
    # overnight gap is then counted as "move since catalyst" even though it
    # happened before we ever saw the catalyst, making entry_band_pct look
    # too tight when the real issue is a stale reference price. This column
    # records which one was actually used so a future calibration pass (see
    # scripts/calibrate_decay_profiles.py) can separate genuine
    # too-tight-band misses from stale-price misses instead of conflating
    # them — it does not change any entry/trigger decision by itself.
    catalyst_price_source = Column(String(16), nullable=True)  # "live"|"close"|"unknown"|None (Tier 3 / pre-migration rows)
    catalyst_ts = Column(DateTime, nullable=False, default=_now)

    horizon_class = Column(String(8), nullable=False)     # "short" | "mid" | "long"
    decay_half_life_days = Column(Float, nullable=False)
    entry_band_pct = Column(Float, nullable=False)        # max move-from-catalyst before entry refused

    source_tier = Column(Integer, nullable=False)         # 1=full pipeline, 2=degraded classify, 3=volume-shock
    conviction_score = Column(Float, nullable=True)       # from upstream score if Tier 1

    status = Column(String(12), nullable=False, default="active")  # active|entered|expired|missed
    missed_reason = Column(String(255), nullable=True)
    expires_at = Column(DateTime, nullable=False)

    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_trade_watchlist_mode_status", "mode", "status"),
        Index("ix_trade_watchlist_mode_sym_type_status", "mode", "symbol", "catalyst_type", "status"),
    )


# ── Intraday-restricted securities (2026-09-11 fix) ─────────────────────────
# Dhan's scrip master carries no "is this symbol allowed to trade INTRADAY"
# flag (T2T/ASM/GSM surveillance restrictions are an exchange-level, often
# temporary status that doesn't show up in the CSV — see execution/
# dhan_client.py's security-cache module note). The only ground truth this
# service actually has is experience: exit_engine already detects the
# rejection reactively (is_security_intraday_restricted_error) when a
# same-day SELL bounces. This table turns that one-time detection into a
# standing, queryable fact so candidate_engine/entry_engine/manual_engine can
# proactively avoid re-picking or re-buying a symbol already known to hit it,
# instead of only ever finding out after a position is already stuck same-day.
# Not scoped by mode — DEMO never talks to Dhan, so every row here originates
# from a REAL rejection, but the underlying exchange restriction applies to
# the symbol regardless of which mode is trading it.
class IntradayRestrictedSecurity(Base):
    __tablename__ = "trade_intraday_restricted"

    symbol = Column(String(32), primary_key=True)
    first_detected_at = Column(DateTime, nullable=False, default=_now)
    last_detected_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    hit_count = Column(Integer, nullable=False, default=1)
    last_detail = Column(String(255), nullable=True)


# ── Resilience — last-known-good cache (real-trade-service's own outbound
# calls and open-positions snapshot per cycle) ───────────────────────────────
class ResilienceCache(Base):
    __tablename__ = "trade_resilience_cache"

    key = Column(String(64), primary_key=True)
    payload_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SharedOrderBudget(Base):
    """Cross-service Dhan account-wide order-rate counter, shared with
    position-stocks-service (same Dhan account, same physical DB — see that
    service's models.py::SharedOrderBudget and
    capital/shared_order_budget.py for the full rationale). Mapped here to
    the SAME table name with matching columns/types so both services'
    create_all() calls agree on its shape regardless of which one boots
    first. See execution/shared_order_budget.py for how this service reads/
    writes it. Table name deliberately NOT prefixed `trade_` (unlike every
    other table in this file) since it's explicitly meant to be shared."""
    __tablename__ = "stockky_shared_order_budget"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, unique=True, index=True)  # 'YYYY-MM-DD' IST
    orders_placed_today = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SharedSymbolLock(Base):
    """Cross-service claim on a symbol currently held at the broker, shared
    with position-stocks-service (same Dhan account, same physical DB —
    see that service's models.py::SharedSymbolLock and
    capital/shared_symbol_lock.py for the full rationale: Dhan holds one
    consolidated position per symbol with no concept of which service's
    shares are whose, which is what let AEGISVOPAK get bought by both
    services on the same day and produce a broker order-type mismatch on
    exit). Mapped here to the SAME table name with matching columns/types
    so both services' create_all() calls agree on its shape regardless of
    which one boots first. See execution/shared_symbol_lock.py for how
    this service reads/writes it. Table name deliberately NOT prefixed
    `trade_` (unlike every other table in this file) since it's explicitly
    meant to be shared."""
    __tablename__ = "stockky_shared_symbol_lock"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, unique=True, index=True)
    held_by_service = Column(String(32), nullable=False)  # "position-stocks-service" | "real-trade-service"
    held_by_mode = Column(String(8), nullable=True)  # this service's REAL/DEMO; null when held by position-stocks-service
    claimed_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class SharedServiceExposure(Base):
    """2026-09-20 audit fix — cross-service open-position market value,
    shared with position-stocks-service (same Dhan account, same physical
    DB — see that service's models.py::SharedServiceExposure and
    capital/shared_exposure.py). Each service publishes ONLY ITS OWN
    open-position market value here; risk_engine/engine.py's
    "capital_share_cap" check reads the OTHER service's row to compute the
    shared account's true total (broker_cash_available + this service's
    own open positions + the other service's open positions), which it
    previously omitted entirely — undercounting the true total whenever
    position-stocks-service held stock, and over-restricting this
    service's 50% cap as a result (the mirror-image of the session52
    incident this whole split exists to prevent, just in the opposite,
    non-money-unsafe direction). See execution/shared_exposure.py for how
    this service reads/writes it. Mapped here to the SAME table name with
    matching columns/types so both services' create_all() calls agree on
    its shape regardless of which one boots first. Table name deliberately
    NOT prefixed `trade_` (unlike every other table in this file) since
    it's explicitly meant to be shared."""
    __tablename__ = "stockky_shared_service_exposure"

    service_name = Column(String(32), primary_key=True)  # "real-trade-service" | "position-stocks-service"
    open_positions_market_value = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


# ── After-hours news → next-day watchlist (2026-09-17, session56) ───────────
# Separate from WatchlistEntry (intraday, same-day decay per watchlist_engine/
# decay.py) — this table persists overnight and survives until market_date's
# _prepick run converts its rows into TradeCandidate rows. watchlist_engine/
# afterhours_scan.py writes here; auto_pilot._prepick reads here.
#
# Design contract:
#   - One row per (mode, symbol, market_date) — upsert on conflict, keep
#     highest priority_score. Multiple headlines for the same symbol on the
#     same night are merged (best score wins).
#   - consumed=False until _prepick injects the row as a TradeCandidate.
#     Once consumed, the row stays for audit (same as WatchlistEntry's
#     status field).
#   - market_date is the IST trading date this row targets (tomorrow's date
#     at scan time). _prepick only reads rows where market_date == today
#     and consumed=False.
class NextDayWatchlistEntry(Base):
    __tablename__ = "trade_nextday_watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(8), nullable=False)   # "DEMO" | "REAL"
    symbol = Column(String(32), nullable=False)

    catalyst_type = Column(String(24), nullable=False)   # "results"|"bulk_block"|"insider"|"board"|"news"
    catalyst_source = Column(String(64), nullable=True)  # "Moneycontrol"|"LiveMint"|"ET"|"NSE-bulk-deals"
    headline = Column(Text, nullable=True)               # best/first matching headline
    priority_score = Column(Float, nullable=False, default=0.0)
    # market_date: IST trading date this entry is FOR (not when it was collected)
    market_date = Column(String(10), nullable=False)     # "YYYY-MM-DD"
    collected_at = Column(DateTime, nullable=False, default=_now)

    consumed = Column(Boolean, nullable=False, default=False)
    consumed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        # Hot-path: _prepick queries mode + market_date + consumed=False
        Index("ix_nextday_watchlist_mode_date_consumed", "mode", "market_date", "consumed"),
        # Upsert-dedup: one row per (mode, symbol, market_date)
        Index("ix_nextday_watchlist_mode_sym_date", "mode", "symbol", "market_date"),
    )
