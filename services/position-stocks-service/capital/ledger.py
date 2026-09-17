"""
capital/ledger.py — ScalpCapitalLedger management.

Enforces the software 50/50 capital split between this service and
real-trade-service. Dhan itself has no concept of sub-pools — this table
IS the split.

On entry:
  1. Sync total_allocated_capital from Dhan's real available balance
     * SCALP_POOL_CAPITAL_SHARE_PCT (once per session or on demand).
  2. Check available_capital >= position_value before allowing a new entry.
  3. Decrement available_capital by position_value atomically in the DB.

On exit (TARGET_HIT / STOP_HIT / EOD_SQUAREOFF):
  4. Increment available_capital by realized_pnl + returned_capital.
  5. Accumulate realized_pnl_today / realized_pnl_total.

Daily loss kill switch:
  6. If realized_pnl_today drops below -(total_allocated_capital *
     MAX_DAILY_LOSS_PCT_OF_POOL / 100), trip the kill switch and refuse
     new entries for the rest of the day.
  This is now based SOLELY on this service's own realized_pnl_today —
  real-trade-service's P&L is tracked separately (sync_peer_pnl(),
  peer_realized_pnl_today) for display only and no longer affects this
  trip decision (see reserve_capital()'s comment for why that cross-
  service coupling was removed).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

import config
from execution import dhan_client
from models import ScalpCapitalLedger, ScalpGateState
from tz_utils import ist_today_str, iso_utc

logger = logging.getLogger("position-stocks-ledger")

# BUG FIX (session53 — live-diagnosed, 2026-09-16): sync_from_broker() used to
# check only 2 of Dhan's known available-balance field names ("availabelBalance"
# — Dhan's own historical typo in the live API, not a typo introduced here —
# and "availableBalance"). Live evidence: real-trade-service's execution/
# equity_sync.py checks 5 known field names and successfully resolved a real
# balance (confirmed live via /status/REAL — cash_available populated) at the
# exact same time this service's /ledger showed last_synced_from_broker_at
# stuck over an hour in the past despite cycles running every 10s and reaching
# the capital-reservation step (proven by fresh INSUFFICIENT_CAPITAL log
# entries) — meaning Dhan is populating this account's funds response under a
# key OUTSIDE this narrower 2-key list, so available_balance silently computed
# to 0 and sync_from_broker() returned early without ever updating
# total_allocated_capital or the timestamp. This is a different failure mode
# from the "50/50 split" and "capital erosion" bugs fixed earlier this session
# — those were about how much capital gets allocated; this one is about the
# sync never actually happening at all, on any cycle, all day. Mirrors
# equity_sync.py's exact key list/order/fallback-tracking so both services
# read Dhan's response the same way.
_BALANCE_KEYS = (
    "availabelBalance", "availableBalance", "availableCash",
    "withdrawableBalance", "sodLimit",
)
_last_balance_key: Optional[str] = None


def _pick_balance(funds: dict) -> tuple[Optional[float], Optional[str]]:
    for key in _BALANCE_KEYS:
        v = funds.get(key)
        if v is None:
            continue
        try:
            return float(v), key
        except (TypeError, ValueError):
            continue
    return None, None


def _get_or_create(db: Session) -> ScalpCapitalLedger:
    row = db.query(ScalpCapitalLedger).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpCapitalLedger(
            mode="REAL",
            total_allocated_capital=0.0,
            available_capital=0.0,
            realized_pnl_today=0.0,
            realized_pnl_total=0.0,
            pnl_last_reset_date=ist_today_str(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    _maybe_lazy_reset_daily(db, row)
    return row


def _maybe_lazy_reset_daily(db: Session, row: ScalpCapitalLedger) -> None:
    """BUG FIX (session13 audit): reset_daily() existed but nothing ever
    called it — no scheduler, no startup hook — so realized_pnl_today
    accumulated across days forever and a tripped daily-loss kill switch
    never cleared on its own. Rather than rely on a scheduled job (which
    would miss the reset entirely if the service happened to be down at
    midnight), every read/write of the ledger row lazily checks whether
    IST's calendar date has moved on since the last reset and, if so,
    performs the same reset reset_daily() already implements before
    returning the row. available_capital is untouched — that carries over
    day to day by design."""
    today = ist_today_str()
    if row.pnl_last_reset_date == today:
        return
    prev_reset_date = row.pnl_last_reset_date
    was_tripped = row.daily_loss_kill_switch_tripped
    prev_pnl = row.realized_pnl_today
    row.realized_pnl_today = 0.0
    row.peer_realized_pnl_today = 0.0  # Issue #2: reset peer cache on new day
    row.daily_loss_kill_switch_tripped = False
    row.daily_loss_kill_switch_tripped_date = None
    row.pnl_last_reset_date = today
    db.commit()
    logger.info(
        "ledger: lazy daily reset applied (last_reset=%s -> %s) — "
        "cleared realized_pnl_today (was ₹%.2f), kill_switch_tripped (was %s)",
        prev_reset_date, today, prev_pnl, was_tripped,
    )


def sync_from_broker(db: Session) -> float:
    """Fetch live fund balance from Dhan, compute 50% scalp allocation,
    store in DB. Returns new total_allocated_capital."""
    try:
        funds = dhan_client.get_funds(db)
    except Exception as e:
        logger.error("ledger.sync_from_broker: failed to get funds: %s", e)
        return 0.0

    available_balance, matched_key = _pick_balance(funds)
    if available_balance is None or available_balance <= 0:
        logger.warning(
            "ledger.sync_from_broker: no usable available-balance field in "
            "Dhan funds response (checked %s): %s",
            _BALANCE_KEYS, funds,
        )
        return 0.0

    global _last_balance_key
    if matched_key != _last_balance_key:
        if _last_balance_key is None:
            logger.warning(
                "ledger.sync_from_broker: using Dhan balance field '%s' for "
                "the scalp pool — verify this reflects a sensible current "
                "tradeable balance.", matched_key,
            )
        else:
            logger.warning(
                "ledger.sync_from_broker: Dhan balance field in use changed "
                "'%s' -> '%s' — the funds response shape shifted; verify the "
                "new field still reflects a sensible tradeable balance.",
                _last_balance_key, matched_key,
            )
        _last_balance_key = matched_key

    scalp_alloc = available_balance * (config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0)
    row = _get_or_create(db)

    # CAPITAL-EROSION FIX (session52 follow-up — "why would the scalp pool
    # ever go hungry again even after the Option A fix?"): this line used
    # to unconditionally set total_allocated_capital = scalp_alloc, i.e.
    # 50% of Dhan's CURRENT FREE CASH, every single sync — including while
    # this pool already has real money committed to its own open
    # positions. Placing an order spends Dhan free cash, so the very next
    # sync would compute a SMALLER scalp_alloc and use it as the new
    # total_allocated_capital, silently shrinking the pool's risk-sizing
    # baseline (risk_rupees = total_allocated_capital * RISK_PER_TRADE_PCT
    # in reserve_capital() below) on every cycle a position stays open —
    # exactly the same "one side of the split doesn't credit back its own
    # committed capital" bug already fixed on real-trade-service's side
    # (see execution/equity_sync.py: current_equity = capped_cash +
    # market_value, not just capped_cash). Mirrored here: add back what
    # this pool has ALREADY committed to its own still-open positions
    # (capital_risked — a cost-basis figure already tracked per position,
    # no live-LTP dependency needed) so total_allocated_capital reflects
    # the pool's TRUE 50% share (idle free cash + its own deployed
    # capital), not just whatever happens to be sitting uncommitted on
    # Dhan at sync time. Doesn't change anything today (Positions (0) means
    # own_committed_capital is currently 0) but prevents the pool from
    # eroding itself the moment it actually starts holding positions.
    from models import ScalpPosition as _SP  # local to avoid circular import
    own_committed_capital = db.query(
        func.coalesce(func.sum(_SP.capital_risked), 0.0)
    ).filter(_SP.status.in_(("OPEN", "EXIT_LEGS_REJECTED"))).scalar()
    row.total_allocated_capital = scalp_alloc + own_committed_capital
    # AUDIT FIX: the original condition `if row.available_capital <= 0`
    # only set available_capital on the very first sync (when the ledger
    # row was brand-new or drained to zero). A second sync call with a
    # LOWER fund balance (e.g. after a withdrawal, or a different total
    # from Dhan's end) would leave available_capital higher than the new
    # total_allocated_capital, so the next reserve_capital() call could
    # allocate more than the pool actually has. Only reset available_capital
    # to the new allocation if NO positions are currently open (i.e. the
    # pool isn't partially reserved) — otherwise leave it alone, because
    # adjusting it while positions are open risks double-counting reserved
    # capital. The correct reconciliation path for an in-flight pool is
    # POST /ledger/sync (which calls this) followed by the operator
    # re-checking /ledger and manually triggering /ledger/reset-daily if
    # the numbers look wrong after all positions close.
    # BUG FIX (audit follow-up to session41b/42): only counted status="OPEN".
    # An EXIT_LEGS_REJECTED position still has capital_risked reserved
    # (release_capital() is never called for it — see reconcile.py, it only
    # flips status/error_message) exactly like an OPEN position does. Missing
    # it here meant open_count could read 0 while capital was still actually
    # committed to a stuck position, hard-resetting available_capital to the
    # full allocation and effectively double-spending that reserved capital.
    # (_SP already imported above for own_committed_capital — reused here.)
    open_count = (
        db.query(_SP)
        .filter(_SP.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .count()
    )
    if open_count == 0:
        # No open positions: own_committed_capital is 0, so total_allocated_
        # capital == scalp_alloc here anyway — safe to hard-reset
        # available_capital to match the freshly synced allocation (keeps
        # the two in sync after a fund balance change).
        row.available_capital = row.total_allocated_capital
    elif row.available_capital <= 0:
        # Open positions exist but available_capital hit zero — at minimum
        # reset to the freshly synced free-cash slice (own_committed_capital
        # is already tied up in those positions, not "available") so the
        # service isn't permanently locked out of new entries after a
        # zero-drain day.
        row.available_capital = scalp_alloc
    row.last_synced_from_broker_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "ledger: synced from broker — total Dhan balance ₹%.2f, scalp pool ₹%.2f",
        available_balance, scalp_alloc,
    )
    # BUG FIX (Issue #2): sync peer PnL at the same cadence as broker sync
    # so reserve_capital()'s combined kill-switch check stays current.
    sync_peer_pnl(db)
    return scalp_alloc


def sync_peer_pnl(db: Session) -> Optional[float]:
    """BUG FIX (Issue #2): fetch real-trade-service's realized_pnl_today and
    cache it in the ledger row so reserve_capital()'s daily-loss kill switch
    accounts for losses on the SAME shared Dhan account booked by the peer
    service. Called from sync_from_broker() at the same cadence — NOT on the
    hot entry path — to avoid adding latency or a single-point-of-failure to
    every trade attempt.

    Fails silently (returns None, logs a warning) if real-trade-service is
    unreachable or returns unexpected data — position-stocks must never be
    blocked from trading by a connectivity issue with its peer. The last
    successfully cached value remains in effect until the next successful sync.
    Returns the fetched peer pnl_today on success, None on failure."""
    import urllib.request
    import json as _json

    try:
        url = f"{config.REAL_TRADE_SERVICE_URL}/status/REAL"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = _json.loads(resp.read())
        peer_pnl = float(data.get("account", {}).get("realized_pnl_today", 0.0))
        row = _get_or_create(db)
        row.peer_realized_pnl_today = peer_pnl
        row.peer_pnl_last_synced_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "ledger.sync_peer_pnl: real-trade-service realized_pnl_today=₹%.2f cached",
            peer_pnl,
        )
        return peer_pnl
    except Exception as e:
        logger.warning(
            "ledger.sync_peer_pnl: could not fetch real-trade-service pnl "
            "(using last cached value): %s", e,
        )
        return None


def reserve_capital(
    db: Session,
    *,
    adaptive_stop_pct: float,
) -> Optional[float]:
    """Gate check + decrement. Returns position_value (₹) if OK, None if
    insufficient capital or kill-switch tripped."""
    row = _get_or_create(db)

    if row.daily_loss_kill_switch_tripped:
        logger.warning("ledger.reserve_capital: daily loss kill switch tripped — refusing entry")
        return None

    # REMOVED (this session, user request): Issue #2's cross-service combined-
    # pnl kill switch used to trip THIS service off real-trade-service's losses
    # too (own + peer against OUR pool). Two things made that wrong in practice:
    # (1) real-trade-service's realized_pnl_today was never actually reset
    #     daily (fixed separately this session, in real-trade-service's own
    #     portfolio.py) — so the "peer" number being compared here was really
    #     an ALL-TIME cumulative figure, not today's, meaning this service
    #     could get permanently frozen by stale historical losses that had
    #     nothing to do with today.
    # (2) even with a correct daily number, comparing the peer's rupee loss
    #     against OUR (smaller) pool rather than the peer's own pool meant a
    #     real-trade-service loss well within ITS OWN daily-loss tolerance
    #     could still trip US.
    # Per explicit user decision: each service now tracks and trips its own
    # kill switch off its OWN realized_pnl_today only (see the plain
    # loss_pct check in release_capital() below) — the two services are
    # fully independent for this purpose. sync_peer_pnl() and
    # peer_realized_pnl_today are KEPT (still synced, still surfaced in
    # /ledger's response) purely as an informational "what is the peer
    # doing" readout — they no longer feed into this trip decision.
    if row.total_allocated_capital <= 0:
        logger.warning("ledger.reserve_capital: total_allocated_capital=0 — run sync_from_broker first")
        return None

    # BUG FIX (#1 — position sizing formula ate 100% of pool per trade):
    # The original formula was:
    #   risk_rupees    = total_allocated_capital * RISK_PER_TRADE_PCT%
    #   position_value = risk_rupees / adaptive_stop_pct%
    # With RISK_PER_TRADE_PCT=2% and MIN_STOP_PCT=2% (the ATR floor),
    # position_value = pool * 2% / 2% = 100% of pool — for ONE trade.
    # But MAX_CONCURRENT_SCALP_POSITIONS=5 means the pool must sustain 5
    # concurrent positions. No division by concurrency existed, so the
    # first position consumed nearly all available capital, leaving every
    # subsequent candidate with INSUFFICIENT_CAPITAL even when ~50% of the
    # pool was still nominally "available". Fixed by dividing risk_rupees
    # (NOT position_value) by MAX_CONCURRENT_SCALP_POSITIONS first, so
    # each slot gets its fair share of the pool's risk budget.
    risk_rupees = (
        row.total_allocated_capital
        * (config.RISK_PER_TRADE_PCT / 100.0)
        / config.MAX_CONCURRENT_SCALP_POSITIONS
    )
    position_value = risk_rupees / (adaptive_stop_pct / 100.0)

    if position_value > row.available_capital:
        logger.info(
            "ledger.reserve_capital: insufficient capital (need ₹%.2f, have ₹%.2f)",
            position_value, row.available_capital,
        )
        return None

    row.available_capital -= position_value
    db.commit()
    logger.info(
        "ledger.reserve_capital: reserved ₹%.2f (risk ₹%.2f / %d slots, stop %.2f%%), remaining ₹%.2f",
        position_value, risk_rupees, config.MAX_CONCURRENT_SCALP_POSITIONS,
        adaptive_stop_pct, row.available_capital,
    )
    return position_value


def reserve_additional(db: Session, additional_amount: float) -> bool:
    """Top up an already-reserved amount by `additional_amount` more.

    BUG FIX (this session): orders/entry.py floors quantity to at least 1
    share (`max(1, int(position_value / current_ltp))`). When the
    risk-sized `position_value` from reserve_capital() above is SMALLER
    than one share's price (a small pool, or a high-priced stock, or
    both), that floor makes the real order cost MORE than what was
    already deducted from available_capital — e.g. ₹3,333 reserved but a
    ₹4,500 stock still needs qty=1, so the real Dhan order commits ₹1,167
    more real rupees than the ledger ever accounted for. Silently letting
    that through would mean available_capital overstates what's actually
    still free — and since this pool is a SOFTWARE-enforced half of one
    real, shared Dhan account, an unaccounted overspend here can eat into
    real-trade-service's half without either service's ledger ever
    reflecting it. This function lets entry.py reserve exactly the real
    shortfall before placing the order (or, if there isn't enough
    available_capital left to cover it, entry.py skips the trade instead
    of placing an order the ledger can't actually back).
    Does NOT re-check the daily-loss kill switch or
    total_allocated_capital>0 — those were already verified by the
    reserve_capital() call this tops up."""
    if additional_amount <= 0:
        return True
    row = _get_or_create(db)
    if additional_amount > row.available_capital:
        return False
    row.available_capital -= additional_amount
    db.commit()
    logger.info(
        "ledger.reserve_additional: topped up reservation by ₹%.2f "
        "(min-quantity-floor shortfall), remaining ₹%.2f",
        additional_amount, row.available_capital,
    )
    return True


def release_capital(
    db: Session,
    *,
    position_value: float,
    realized_pnl: float,
) -> None:
    """Return capital + P&L on exit. Checks daily-loss kill switch."""
    row = _get_or_create(db)
    row.available_capital += position_value + realized_pnl
    row.realized_pnl_today += realized_pnl
    row.realized_pnl_total += realized_pnl

    # Daily loss kill switch
    if row.total_allocated_capital > 0:
        loss_pct = abs(min(row.realized_pnl_today, 0)) / row.total_allocated_capital * 100
        if loss_pct >= config.MAX_DAILY_LOSS_PCT_OF_POOL and not row.daily_loss_kill_switch_tripped:
            row.daily_loss_kill_switch_tripped = True
            row.daily_loss_kill_switch_tripped_date = ist_today_str()
            logger.warning(
                "DAILY LOSS KILL SWITCH TRIPPED: realized_pnl_today=₹%.2f "
                "(%.1f%% of pool ₹%.2f). No new entries today.",
                row.realized_pnl_today, loss_pct, row.total_allocated_capital,
            )
            # BUG FIX (this session): this only ever set the LEDGER's own
            # copy of the flag. models.py's own docstring on
            # ScalpGateState.daily_loss_kill_switch_tripped already
            # documents these as "two entirely disconnected copies of the
            # same concept" and session13 fixed them drifting apart on
            # RESET (the lazy reset-on-date-change) — but nothing ever
            # fixed them drifting apart on TRIP: this is the actual real
            # trading-loss trip path (as opposed to the manual /kill
            # route, which sets the gate's copy directly), and it left
            # gate.daily_loss_kill_switch_tripped permanently False. Since
            # GET /status (the dashboard's Status/Risk card) reads ONLY
            # the gate's copy — not the ledger's, which GET /ledger shows
            # separately — an operator would see "Daily Loss Kill Switch:
            # not tripped" on the main status card even while the ledger
            # has already correctly stopped every new entry at the
            # capital-reservation step. Entries were never actually at
            # risk either way (reserve_capital() above already checks the
            # ledger's own copy directly, independent of the gate) — this
            # fixes the dashboard/operator-visibility gap, not a trading
            # safety gap.
            gate = db.query(ScalpGateState).filter_by(mode="REAL").first()
            if gate is not None and not gate.daily_loss_kill_switch_tripped:
                gate.daily_loss_kill_switch_tripped = True
                gate.daily_loss_kill_switch_tripped_date = row.daily_loss_kill_switch_tripped_date

    db.commit()
    logger.info(
        "ledger.release_capital: returned ₹%.2f + P&L ₹%.2f, available=₹%.2f, "
        "pnl_today=₹%.2f",
        position_value, realized_pnl, row.available_capital, row.realized_pnl_today,
    )


def reconcile_position_cost(db: Session, *, delta: float) -> None:
    """AUDIT FIX (this session — the flagged "capital_risked has the same
    staleness as entry_price" follow-up from the prior audit pass):
    capital_risked is reserved at entry time from the same pre-order LTP
    estimate as entry_price (`orders/entry.py`'s `position_value`), and
    was never corrected once Dhan's real average fill price became known
    — same root cause as the entry_price bug fixed last session, just on
    the capital-accounting side instead of the P&L-reporting side. Once
    orders/reconcile.py corrects a position's entry_price and
    capital_risked to the real fill (quantity * real_entry_price), the
    ledger's available_capital needs the matching adjustment so the two
    stay consistent — otherwise release_capital() at exit would return
    (stale capital_risked + now-correct realized_pnl), which no longer
    nets to the real sale proceeds (exit_price * quantity) the way it's
    supposed to.

    `delta` = new_real_cost - old_capital_risked, computed by the caller.
    Positive delta (the real Dhan fill cost MORE than what this pool
    reserved for it) further deducts the shortfall from available_capital
    — those rupees were already spent for real on Dhan's shared account
    regardless of whether this software pool "has" them, so this can
    legitimately push available_capital negative. That's the honest
    signal of a real overspend eating into real-trade-service's half of
    the same account (the exact risk reserve_additional()'s docstring
    already describes for the separate min-quantity-floor case) — NOT a
    bug to hide by clamping at zero. Negative delta (the real fill cost
    LESS than reserved) returns the freed-up excess back to the pool,
    same as any other capital release.

    Does NOT touch the daily-loss kill switch — that trips off
    realized_pnl_today in release_capital() at actual exit time, not off
    an in-flight cost correction on a still-open position."""
    if delta == 0:
        return
    row = _get_or_create(db)
    row.available_capital -= delta
    db.commit()
    if row.available_capital < 0:
        logger.warning(
            "ledger.reconcile_position_cost: available_capital went "
            "negative (₹%.2f) after a ₹%.2f real-fill-cost correction — "
            "the real Dhan entry cost more than this pool had reserved "
            "for it.",
            row.available_capital, delta,
        )
    else:
        logger.info(
            "ledger.reconcile_position_cost: available_capital adjusted "
            "by ₹%.2f for a real-fill-cost correction, now ₹%.2f",
            -delta, row.available_capital,
        )


def reclaim_premature_release(db: Session, *, capital_risked: float) -> None:
    """BUG FIX (audit follow-up): orders/eod_squareoff.py releases a
    position's capital_risked back into available_capital immediately
    after successfully PLACING the flat MARKET SELL — before Dhan has
    confirmed any fill — so the position shows CLOSED on the dashboard
    right away (see its own comment: "exit price is unknown here... We
    record entry_price as a placeholder"). That is correct for the
    common case (the SELL later fills, per the placeholder-then-
    reconcile design), but orders/reconcile.py's _reconcile_eod_pending
    can also discover the SELL came back REJECTED/CANCELLED with ZERO
    fill — meaning the position is still genuinely open at the broker,
    with real capital still at risk there, even though this ledger
    already gave that capital back as "available" the moment the order
    was placed. Left uncorrected, available_capital overstates what's
    actually free by exactly capital_risked, letting a new position be
    opened against capital that's still committed to the zombie one.

    Called only from that DEAD_EXIT_STATUSES path, once per position
    (reconcile.py marks the position ERROR right after, so this cannot
    re-fire for the same row). Mirrors reconcile_position_cost's delta
    mechanics but is named for what it actually does here: undoing an
    earlier release_capital() call that turned out to be premature, not
    correcting a cost estimate. Does NOT touch realized_pnl (no trade
    actually happened) or the daily-loss kill switch (no loss booked)."""
    if capital_risked <= 0:
        return
    row = _get_or_create(db)
    row.available_capital -= capital_risked
    db.commit()
    logger.warning(
        "ledger.reclaim_premature_release: EOD flat-SELL never filled — "
        "reclaiming ₹%.2f that was released early, available=₹%.2f "
        "(position is still open at the broker; needs manual review)",
        capital_risked, row.available_capital,
    )


def reset_daily(db: Session) -> None:
    """Manual/explicit reset of daily P&L and kill switch — e.g. an admin
    route for testing or an emergency override. Does NOT reset
    available_capital (that carries over). Note: this is no longer the
    only way the daily reset happens — _get_or_create() now also performs
    it lazily on the first ledger access after IST's calendar date rolls
    over (see _maybe_lazy_reset_daily), so this function is a manual
    trigger on top of that automatic path, not a replacement for it."""
    row = _get_or_create(db)  # also applies the lazy reset if due
    row.realized_pnl_today = 0.0
    row.peer_realized_pnl_today = 0.0  # Issue #2: reset peer cache on manual reset too
    row.daily_loss_kill_switch_tripped = False
    row.daily_loss_kill_switch_tripped_date = None
    row.pnl_last_reset_date = ist_today_str()
    db.commit()
    logger.info("ledger.reset_daily: daily P&L and kill switch reset (manual trigger)")


def get_state(db: Session) -> dict:
    row = _get_or_create(db)
    return {
        "total_allocated_capital": row.total_allocated_capital,
        "available_capital": row.available_capital,
        "realized_pnl_today": row.realized_pnl_today,
        "realized_pnl_total": row.realized_pnl_total,
        "peer_realized_pnl_today": row.peer_realized_pnl_today,  # Issue #2: real-trade-service's pnl
        "peer_pnl_last_synced_at": iso_utc(row.peer_pnl_last_synced_at),
        # AUDIT FIX (this session): same raw-datetime gap as main.py's
        # other endpoints — see the comment on GET /status for the full
        # reasoning. Without iso_utc(), this timestamp displays off by
        # +5:30 (IST) in any frontend consuming it.
        "last_synced_from_broker_at": iso_utc(row.last_synced_from_broker_at),
        "daily_loss_kill_switch_tripped": row.daily_loss_kill_switch_tripped,
    }
