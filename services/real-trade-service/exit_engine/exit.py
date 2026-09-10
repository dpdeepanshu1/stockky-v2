"""
exit_engine/exit.py

MARKET INTELLIGENCE APPLIED TO EXIT LOGIC (28-Aug-2026):
═════════════════════════════════════════════════════════
Nifty in correction (−7% in 6m). FIIs net short 1,97,792 contracts.
Midcap/Smallcap outperforming — DII buying provides a floor at 24,000.

What this means for exits:
  1. PROTECT PROFITS FASTER: in a choppy/weak market, open gains evaporate
     quickly. Partial exit raised to 60% (was 50%) at first target — lock
     in more when you have it. The remaining 40% still rides the trail.

  2. AGE-AWARE TRAILING STOP: trail ATR multiplier tightens as trade ages.
     Day 0–3: 2.0×ATR (let the trade breathe, avoid noise-stop).
     Day 4–7: 1.5×ATR (original — standard phase).
     Day 8+:  1.0×ATR (very tight — protect accumulated profit).
     In a choppy market, a trade that hasn't hit target by day 8 is likely
     churning. Tighten the trail and be ready to exit.

  3. BREAKEVEN STOP: once unrealized gain ≥ 1×ATR, automatically move
     stop to entry price. This creates a "free ride" — if the trade
     reverses from here, we exit at breakeven, not a loss. Critical in a
     choppy market where moves can reverse sharply.

  4. SHORTER TIME-STOP: 10 days (unchanged from original). But now there's
     an EARLY WARNING at day 6: if still below entry, log a HOLD decision
     with a note. At day 10, if not profitable, exit — capital shouldn't
     sit dead when midcap/smallcap opportunities are turning over faster.

  5. GAP-DOWN EMERGENCY EXIT: if unrealized loss exceeds 1.5× the original
     stop distance (gap-through scenario), exit IMMEDIATELY regardless of
     current_stop level. In a weak market, gap-downs are common and a stop
     that was "breached but not hit" needs catching.

  6. TARGET NULLIFIED AFTER PARTIAL: after taking the first partial exit,
     current_target is set to None so the remainder is trailed indefinitely
     rather than re-triggering at a stale target price.

All REAL-mode Dhan placement, IP guard, audit trail unchanged from original.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import models
from audit.logger import log_action
from execution import dhan_client
from market_feed.feed import get_quotes
from notifier import notify_sync
from portfolio.portfolio import (
    close_position, force_close_real_position, open_positions, refresh_unrealized, record_real_exit_sent,
)
from resilience.local_cache import load_snapshot, save_snapshot
from tz_utils import as_aware, ist_today_str

# §6 — corporate-action clamp for ATR trailing stop inputs
try:
    from return_sanity import clamp_for_atr as _clamp_for_atr
except ImportError:
    def _clamp_for_atr(x):
        return None if x is None or abs(x) > 30.0 else x

logger = logging.getLogger("real-trade-exit")

# ── Exit constants — market-intelligence tuned ────────────────────────────────
# Lock in 60% at first target (was 50%) — in choppy market, don't let
# profits turn into losses. The 40% remainder rides an ever-tightening trail.
PARTIAL_EXIT_FRACTION = float(os.getenv("EXIT_PARTIAL_FRACTION", "0.60"))

# Age → ATR multiplier mapping for trailing stop.
# Younger positions need more room; older ones should be protecting profit.
# Format: list of (max_days_inclusive, atr_multiplier).
TRAIL_ATR_SCHEDULE = [
    (3,  2.0),   # day 0–3:  2×ATR — let the trade breathe through noise
    (7,  1.5),   # day 4–7:  1.5×ATR — standard, same as original default
    (99, 1.0),   # day 8+:   1×ATR — tight, protect accumulated gains
]

# Breakeven stop: move stop to entry once unrealized gain >= this many ATRs.
BREAKEVEN_ATR_TRIGGER = float(os.getenv("EXIT_BREAKEVEN_ATR_TRIGGER", "1.0"))

# Emergency exit: fire if unrealized loss > this × original stop distance.
# Catches gap-down scenarios where price breaks through the stop level.
EMERGENCY_LOSS_MULT = float(os.getenv("EXIT_EMERGENCY_LOSS_MULT", "1.5"))

# Time-stop: max days to hold a non-performing position.
MAX_HOLD_DAYS = int(os.getenv("EXIT_MAX_HOLD_DAYS", "10"))
# Day at which we log an early warning (no exit yet, just visibility).
EARLY_WARN_DAYS = int(os.getenv("EXIT_EARLY_WARN_DAYS", "6"))

# 2026-09-07 fix: CDSL eDIS/TPIN rejections (see dhan_client.is_cdsl_edis_error's
# docstring) are not a transient/retryable-into-success failure the same
# way an IP or a broker hiccup is — they need a human to complete a manual
# CDSL step, and will keep failing every cycle until that happens. Without
# a cooldown, that means one Telegram alert PER SYMBOL PER CYCLE for as
# long as the position stays open and unauthorized — exactly the 6h+ of
# identical repeated alerts this was found from. Throttle to one alert per
# position per this many minutes; the underlying retry (still attempted
# every cycle, in case the human completes the CDSL step mid-day) is
# unaffected — only the notification is throttled.
CDSL_ALERT_COOLDOWN_MIN = int(os.getenv("EXIT_CDSL_ALERT_COOLDOWN_MIN", "60"))

# 2026-09-07: after this many CONSECUTIVE unrecognized SELL rejections for
# the same position, escalate to a distinctly-worded alert (see the generic
# rejection branch in _send_real_sell) instead of sending an identical
# "rejected" message every single cycle forever.
EXIT_REJECT_STREAK_ESCALATE_AT = int(os.getenv("EXIT_REJECT_STREAK_ESCALATE_AT", "3"))


def _trail_atr_mult(held_days: int, schedule=None) -> float:
    """Return the ATR multiplier for trailing stop based on how long
    the position has been held. Tightens over time to protect profits.
    Accepts an optional schedule override (per-catalyst-horizon profile);
    falls back to the module-level TRAIL_ATR_SCHEDULE."""
    s = schedule if schedule is not None else TRAIL_ATR_SCHEDULE
    for max_days, mult in s:
        if held_days <= max_days:
            return mult
    return s[-1][1]


# ── Short-Term Trading Upgrade (2026-09-02): per-position exit profile ────────
# Positions opened from a WatchlistEntry carry watchlist_entry_id, which lets
# us look up the catalyst's horizon_class and apply a tighter or looser exit
# profile. Manual trades (watchlist_entry_id=None) fall back to the existing
# module-level constants above — NO behavior change for them.

def _load_profile(db: Session, position) -> dict:
    """
    Return the exit profile dict for this position.
    Keys match exit.py's module constants: trail_atr_schedule,
    breakeven_atr_trigger, max_hold_days, early_warn_days, partial_exit_fraction.
    """
    from watchlist_engine.decay import exit_profile_for

    if getattr(position, "watchlist_entry_id", None) is None:
        # Manual or pre-upgrade position — use existing global defaults.
        return {
            "trail_atr_schedule":    TRAIL_ATR_SCHEDULE,
            "breakeven_atr_trigger": BREAKEVEN_ATR_TRIGGER,
            "max_hold_days":         MAX_HOLD_DAYS,
            "early_warn_days":       EARLY_WARN_DAYS,
            "partial_exit_fraction": PARTIAL_EXIT_FRACTION,
            "horizon_class":         None,
        }
    try:
        row = db.query(models.WatchlistEntry).get(position.watchlist_entry_id)
        horizon_class = row.horizon_class if row else None
    except Exception:
        horizon_class = None
    profile = exit_profile_for(horizon_class)
    return {**profile, "horizon_class": horizon_class}


def _write_exit_decision(
    db: Session,
    position: models.TradePosition,
    action: str,
    reasoning: str,
    ltp: float,
) -> None:
    """Write an audit exit decision row. Every evaluation — including HOLD —
    gets logged per the plan's audit principle."""
    db.add(models.TradeExitDecision(
        position_id=position.id,
        action=action,
        reasoning=reasoning,
        ltp_at_decision=ltp,
    ))


def _has_pending_real_sell(db: Session, symbol: str) -> bool:
    """True if a REAL SELL for this symbol is already placed and awaiting
    broker confirmation — prevents double-selling before reconcile runs.
    Includes "PARTIAL" (Dhan PART_TRADED — reconcile.py) alongside
    "PLACED": a SELL that's only partially filled is still awaiting the
    rest of its own fill at the broker, so it must keep blocking a second
    SELL the exact same way an unfilled one does — otherwise this system
    could send another market SELL for the same remaining qty while
    Dhan's own order is still working."""
    return (
        db.query(models.TradeOrder)
        .filter(
            models.TradeOrder.mode == "REAL",
            models.TradeOrder.symbol == symbol,
            models.TradeOrder.side == "SELL",
            models.TradeOrder.status.in_(("PLACED", "PARTIAL")),
        )
        .first()
        is not None
    )


def _send_real_sell(
    db: Session,
    position: models.TradePosition,
    qty: int,
    reason: str,
    full: bool = True,
    execution_source: str = "AUTO",
    confirmed_by: Optional[str] = None,
) -> bool:
    """Place a MARKET SELL at Dhan for `qty` shares of an open REAL position.
    MARKET (not LIMIT) — an exit's purpose is capital protection; a limit
    sell that never fills defeats that.

    Returns True only if Dhan accepted and returned an order id.
    On failure the position is left untouched so exit_engine retries next cycle.
    execution_source/confirmed_by: set by manual_engine.py for human-initiated
    sells; left at AUTO defaults for all automatic exit logic.

    2026-09-08 fix: this always sent product_type="CNC" (the place_order
    default), which is correct for a position opened on an earlier trading
    day — those shares have already settled into the demat account and are
    real "holdings". It is WRONG for a position opened and exited the SAME
    day (stop_hit / target_hit_partial / emergency_gap_down firing shortly
    after entry are exactly this case): CDSL only credits a buy to the
    demat account one working day later, so at the moment of a same-day
    exit the shares aren't holdings yet — Dhan rejects the CNC SELL with
    "Dhan API error: Validate Qty from CDSL" because there is nothing in
    CDSL for it to validate the quantity against. (This is also why Dhan
    requires a fresh CDSL eDIS/TPIN step to sell existing holdings at all —
    an interactive one-time-password step this fully automated service has
    no way to complete, and one that in any case only covers shares CDSL
    already knows about, never same-day ones.) A same-day round trip must
    instead be sold as product_type="INTRADAY", which settles net against
    the day's own buy and never touches CDSL holdings validation at all.

    2026-09-09 fix ("insufficient funds" on SELL — see models.py
    TradePosition.broker_imported docstring): the same_day check below reads
    position.opened_at, but for a position brought in by
    portfolio.import_broker_holdings, opened_at is the IMPORT timestamp, not
    the real purchase date — the Dhan holdings API doesn't expose that.
    Importing today made every pre-existing holding look like a same-day
    round trip and get sold product_type="INTRADAY". Dhan has no MIS
    position to net that against, so it priced the SELL as a fresh naked
    short and margin-rejected it with "insufficient funds" — a completely
    different failure from the CDSL-eDIS case above, but easy to mistake for
    it since both surface as a same-day SELL rejection. broker_imported is
    checked FIRST and forces CNC unconditionally, because a holding this
    system never bought itself is never a same-day round trip regardless of
    what opened_at says.

    2026-09-10 fix (session21e, live-evidence-driven): the intraday-cutoff
    branch below already knew a rejection there "can never succeed" again
    today, but only throttled the *alert* about it — nothing stopped the
    *resend* itself. Live Dhan order-book evidence: dozens of "Intraday
    orders cannot be placed at this time" rejections clustered right around
    EOD_SQUAREOFF_TIME_IST (15:15) firing and then the 45s fast-exit loop
    (EXIT_CHECK_INTERVAL_SECONDS) resending the same doomed SELL for the
    same still-open position every cycle for the ~10-15 minutes left before
    close — very plausibly most of a 205-failed-orders count in one session.
    Now short-circuits before ever calling Dhan again for a position that
    already hit the cutoff earlier this IST day (see the snapshot check
    right below the same-day/product_type decision above)."""
    _cutoff_key = f"intraday_cutoff_hit_{position.id}_{ist_today_str()}"
    if load_snapshot(db, _cutoff_key):
        logger.info(
            "Skipping SELL for %s (%s) — already hit today's intraday "
            "cutoff earlier this session; will retry tomorrow as CNC.",
            position.symbol, reason,
        )
        return False
    if position.broker_imported:
        same_day_position = False
    else:
        same_day_position = ist_today_str(as_aware(position.opened_at)) == ist_today_str()
    sell_product_type = "INTRADAY" if same_day_position else "CNC"
    # 2026-09-07: permanent visibility into this decision — session21's
    # investigation had to reconstruct this after the fact from a DB query
    # because nothing logged it at decision time. Now every attempt (success
    # or failure) leaves this line in the logs.
    logger.info(
        "exit SELL %s x%s (%s): opened_at=%s -> IST day %s (today=%s) -> product_type=%s",
        position.symbol, qty, reason, position.opened_at,
        ist_today_str(as_aware(position.opened_at)), ist_today_str(), sell_product_type,
    )
    try:
        security_id = dhan_client.get_security_id(db, position.symbol)
        result = dhan_client.place_order(
            db, is_armed=True,   # exits always allowed — never blocked by armed state
            security_id=security_id,
            exchange_segment=dhan_client.NSE_EQ_SEGMENT,
            transaction_type="SELL",
            quantity=qty,
            order_type="MARKET",
            price=0,
            product_type=sell_product_type,
        )
        dhan_order_id = str(result.get("orderId") or result.get("order_id") or "")
        if not dhan_order_id:
            raise RuntimeError(
                f"Dhan accepted the SELL but returned no order id: {result}"
            )

        order = models.TradeOrder(
            mode="REAL", symbol=position.symbol, side="SELL", order_type="MARKET",
            qty=qty, status="PLACED", dhan_order_id=dhan_order_id,
            execution_source=execution_source,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(timezone.utc) if confirmed_by else None,
            exit_reason=reason,
        )
        db.add(order)
        db.flush()
        db.add(models.TradeOrderEvent(
            order_id=order.id, event_type="PLACED",
            detail=f"{reason}: MARKET SELL {qty} sent to Dhan ({sell_product_type})",
        ))
        record_real_exit_sent(db, position, dhan_order_id, qty, reason, full=full)
        # 2026-09-07: a successful placement means any prior repeated-
        # rejection streak for this position is over — clear it so a later
        # rejection (if the position re-enters trouble another way) starts
        # counting fresh rather than inheriting today's count.
        save_snapshot(db, f"exit_reject_streak_{position.id}", {"count": 0})
        # ENRICHMENT (2026-09-02): previously just symbol + qty + reason, no
        # price at all. Now includes entry price, current stop/target, and
        # last-mark unrealized P&L so the Telegram alert is actionable on
        # its own without needing to open the dashboard.
        _stop_txt = f"₹{position.current_stop:.2f}" if position.current_stop is not None else "—"
        _target_txt = f"₹{position.current_target:.2f}" if position.current_target is not None else "—"
        notify_sync(
            f"📤 *SELL sent* — {position.symbol} ×{qty} ({reason})\n"
            f"Entry: ₹{position.avg_entry_price:.2f} | Stop: {_stop_txt} | Target: {_target_txt}\n"
            f"Unrealized P&L: ₹{position.unrealized_pnl:,.2f}\n"
            "Awaiting broker fill confirmation."
        )
        return True

    except Exception as e:
        logger.error("REAL exit SELL failed for %s (%s): %s", position.symbol, reason, e)
        if dhan_client.is_invalid_ip_error(str(e)):
            from auth.dhan_credentials import disarm_on_invalid_ip
            just_disarmed = disarm_on_invalid_ip(db, "REAL", str(e))
            notify_sync(
                (
                    f"🚨 *EXIT BLOCKED — IP not whitelisted* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    "Dhan rejected this SELL. Position still open and exposed. "
                    "REAL auto-paused. Check GET /dhan/network-check."
                ) if just_disarmed else (
                    f"⚠️ *EXIT still blocked (IP)* — "
                    f"{position.symbol} ×{qty} ({reason}) — position remains open."
                )
            )
        elif dhan_client.is_cdsl_edis_error(str(e)):
            # 2026-09-07 fix: see dhan_client.is_cdsl_edis_error's docstring
            # for why this specifically needs a human, not a retry, and
            # CDSL_ALERT_COOLDOWN_MIN's comment above for why this branch
            # is throttled instead of alerting every cycle like the raw
            # logs showed happening for 6h+ straight.
            snap_key = f"cdsl_alert_last_{position.id}"
            last = load_snapshot(db, snap_key) or {}
            last_at_raw = last.get("at")
            due = True
            if last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    elapsed_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            if due:
                notify_sync(
                    f"🔒 *EXIT BLOCKED — CDSL authorization required* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    "Dhan needs a CDSL eDIS/TPIN 'Verify Holdings' step "
                    "before it will sell this holding — this is a SEBI-"
                    "mandated manual step (OTP to your registered mobile), "
                    "not something this service can complete unattended.\n"
                    "Open the Dhan app -> Verify Holdings, enter the TPIN "
                    "sent to your phone, then this will clear on the next "
                    "retry cycle. Position remains open until then.\n"
                    f"(Next alert for this position suppressed for "
                    f"{CDSL_ALERT_COOLDOWN_MIN} min — retries continue silently.)"
                )
                save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})
            else:
                logger.info(
                    "CDSL block persists for %s (%s) — alert suppressed, "
                    "still within cooldown.", position.symbol, reason,
                )
        elif dhan_client.is_insufficient_funds_error(str(e)):
            # 2026-09-09 fix: see dhan_client.is_insufficient_funds_error's
            # docstring. The common cause (a broker_imported holding sold as
            # INTRADAY with no MIS position to net against, so Dhan margins
            # it like a fresh short) is fixed above via product_type — this
            # branch now mainly covers a genuine margin shortfall, or a
            # pre-fix position that hasn't been re-evaluated yet. Reuses the
            # same per-position cooldown key/idiom as the CDSL branch so a
            # persistent shortfall doesn't page every cycle.
            snap_key = f"funds_alert_last_{position.id}"
            last = load_snapshot(db, snap_key) or {}
            last_at_raw = last.get("at")
            due = True
            if last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    elapsed_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            if due:
                notify_sync(
                    f"💰 *EXIT BLOCKED — insufficient funds* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    f"Dhan margin-rejected this SELL: {str(e)[:200]}\n"
                    "This SELL was sent as a fresh margin position, not a "
                    "square-off of an existing broker position — normally "
                    "because Dhan has no matching intraday (MIS) position to "
                    "net it against. Position remains open; will retry next "
                    "cycle.\n"
                    f"(Next alert for this position suppressed for "
                    f"{CDSL_ALERT_COOLDOWN_MIN} min — retries continue silently.)"
                )
                save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})
            else:
                logger.info(
                    "Insufficient-funds block persists for %s (%s) — alert "
                    "suppressed, still within cooldown.", position.symbol, reason,
                )
        elif dhan_client.is_oversell_error(str(e)):
            # 2026-09-09 fix: "sell more than the quantity you currently hold"
            # — our qty_open is stale vs broker demat. Fetch live holdings,
            # sync qty_open, ghost-close if broker shows 0. Never streak.
            logger.warning(
                "exit SELL %s: oversell rejected (our qty=%s) — fetching "
                "live holdings to sync qty_open", position.symbol, qty,
            )
            try:
                # BUG FIX (2026-09-09, found this session): this previously
                # called `get_dhan_client(db).get_holdings()` — but
                # `get_dhan_client` does not exist anywhere in
                # execution/dhan_client.py (there is only `_get_sdk_client`,
                # a private helper). That import has been raising
                # ImportError on every single retry since the sync branch
                # was added, silently caught by the `except Exception as
                # sync_e` below and logged as "holdings sync failed" —
                # meaning qty_open was NEVER actually corrected and the
                # oversell error kept repeating forever with the stale
                # qty. `dhan_client.get_holdings(db)` (module-level
                # function, already imported as `dhan_client` at the top
                # of this file, same call pattern as
                # `dhan_client.is_oversell_error` right above) returns the
                # holdings list directly — no `.get("data")` unwrap needed.
                holdings = dhan_client.get_holdings(db) or []
                broker_qty = 0
                for h in holdings:
                    sym = (h.get("tradingSymbol") or "").upper().replace(" ", "")
                    our_sym = position.symbol.upper().replace(" ", "")
                    if sym == our_sym or sym.startswith(our_sym):
                        broker_qty = int(h.get("availableQty") or h.get("totalQty") or 0)
                        break
                if broker_qty <= 0:
                    logger.warning(
                        "exit SELL %s: broker holds 0 — ghost-closing (id=%s)",
                        position.symbol, position.id,
                    )
                    # BUG FIX (2026-09-10, session21 audit): this called
                    # close_position() — DEMO-only, raises on any REAL
                    # position, which this always is — so the ghost-close
                    # never actually happened; see
                    # portfolio.force_close_real_position's docstring for
                    # the full incident. force_close_real_position is the
                    # REAL-mode equivalent, added this session specifically
                    # for this call site.
                    force_close_real_position(db, position, "oversell_ghost_close")
                    notify_sync(
                        f"🔄 *Position synced* — {position.symbol} ×{qty} ({reason})\n"
                        f"Dhan holds 0 shares but Stockky had qty={qty} open. "
                        f"Force-closed as ghost (broker already exited)."
                    )
                elif broker_qty < position.qty_open:
                    logger.warning(
                        "exit SELL %s: broker holds %s < our %s — capping qty_open",
                        position.symbol, broker_qty, position.qty_open,
                    )
                    position.qty_open = broker_qty
                    db.flush()
                    notify_sync(
                        f"🔄 *Qty synced* — {position.symbol}: "
                        f"was {qty}, broker holds {broker_qty}. Retrying next cycle."
                    )
                else:
                    logger.warning(
                        "exit SELL %s: oversell but broker holds %s >= ours — timing issue, retry",
                        position.symbol, broker_qty,
                    )
            except Exception as sync_e:
                logger.warning(
                    "exit SELL %s: oversell — holdings sync failed (%s), retry next cycle",
                    position.symbol, sync_e,
                )
                # BUG FIX (2026-09-09): the sync above silently failing
                # (e.g. the ImportError this session's fix removed) had
                # NO alert path at all — a position could stay stuck with
                # a stale qty_open indefinitely and nothing would ever
                # surface it. Same throttled-alert idiom as the other
                # branches here, so a future sync failure (holdings API
                # down, bad credentials, etc.) is visible within one
                # cooldown window instead of failing quietly forever.
                snap_key = f"oversell_sync_fail_alert_last_{position.id}"
                last = load_snapshot(db, snap_key) or {}
                due = True
                if last.get("at"):
                    try:
                        elapsed_min = (
                            datetime.now(timezone.utc) -
                            datetime.fromisoformat(last["at"])
                        ).total_seconds() / 60.0
                        due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                    except Exception:
                        due = True
                if due:
                    notify_sync(
                        f"⚠️ *Qty sync failed* — {position.symbol} ×{qty} ({reason})\n"
                        f"Broker rejected SELL as oversell, but fetching live "
                        f"holdings to reconcile also failed: {str(sync_e)[:200]}\n"
                        f"qty_open is still stale — will keep retrying, but "
                        f"this needs a look if it repeats.\n"
                        f"(Alerts suppressed for {CDSL_ALERT_COOLDOWN_MIN} min.)"
                    )
                    save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})

        elif dhan_client.is_exchange_not_allowed_error(str(e)):
            # 2026-09-09 fix: EXCH:16387 "Security not allowed to trade in
            # this market" — T+1 settlement not done yet, exchange intraday
            # window also closed. Nothing works today; leave open, retry
            # tomorrow when CNC is valid. One alert per cooldown window.
            snap_key = f"exch_not_allowed_alert_last_{position.id}"
            last = load_snapshot(db, snap_key) or {}
            due = True
            if last.get("at"):
                try:
                    elapsed_min = (
                        datetime.now(timezone.utc) -
                        datetime.fromisoformat(last["at"])
                    ).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            if due:
                notify_sync(
                    f"⏰ *EXIT BLOCKED — T+1 pending* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    f"Stock bought today hasn't settled to demat yet (T+1) "
                    f"and the intraday window is also closed. "
                    f"Will retry tomorrow as CNC (needs CDSL eDIS/TPIN).\n"
                    f"(Alerts suppressed for {CDSL_ALERT_COOLDOWN_MIN} min.)"
                )
                save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})
            else:
                logger.info("EXCH:16387 block for %s — alert suppressed.", position.symbol)

        elif dhan_client.is_intraday_cutoff_error(str(e)):
            # 2026-09-08 fix: see dhan_client.is_intraday_cutoff_error's
            # docstring — this is Dhan/NSE's own hard end-of-day cutoff for
            # fresh INTRADAY orders (~15:20-15:25 IST), not a recoverable
            # rejection. Retrying the identical order later today can never
            # succeed, so — unlike the generic branch below — this doesn't
            # count toward EXIT_REJECT_STREAK_ESCALATE_AT at all; escalating
            # "N consecutive rejections" for something that is expected to
            # keep failing until tomorrow just trains everyone to ignore the
            # alert. One clear notification per position per day instead
            # (reuses the same cooldown idiom as CDSL/funds above), and the
            # position is left OPEN and untouched: tomorrow it's no longer
            # same-day, so _send_real_sell will naturally send it as CNC
            # (which then needs CDSL eDIS clearance like any other holding).
            snap_key = f"intraday_cutoff_alert_last_{position.id}"
            last = load_snapshot(db, snap_key) or {}
            last_at_raw = last.get("at")
            due = True
            if last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    elapsed_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            # 2026-09-10 fix (session21e): set the resend-suppression flag
            # checked at the top of this function, regardless of whether
            # this particular call is the one that also fires the (still
            # cooldown-throttled) Telegram alert below — the two concerns
            # are independent: "have we already told a human" vs "should we
            # still be hammering Dhan with this."
            save_snapshot(db, _cutoff_key, {"hit": True})
            if due:
                notify_sync(
                    f"⏰ *EXIT BLOCKED — past today's intraday cutoff* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    "Dhan/NSE stop accepting fresh INTRADAY orders shortly "
                    "before market close (~15:20-15:25 IST) — this isn't a "
                    "credentials/eDIS issue, and retrying today won't help. "
                    "Position remains open overnight; tomorrow this is no "
                    "longer a same-day trade so the exit will be sent as a "
                    "regular delivery (CNC) sell instead — which will need "
                    "the usual CDSL 'Verify Holdings' TPIN step, same as any "
                    "other holding.\n"
                    f"(Further alerts for this position suppressed for "
                    f"{CDSL_ALERT_COOLDOWN_MIN} min. As of the 2026-09-10 "
                    f"fix, Dhan is no longer resent this doomed order every "
                    f"cycle either — this position is simply skipped for the "
                    f"rest of today.)"
                )
                save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})
            else:
                logger.info(
                    "Intraday-cutoff block persists for %s (%s) — alert "
                    "suppressed, still within cooldown.", position.symbol, reason,
                )

        elif dhan_client.is_security_intraday_restricted_error(str(e)):
            # BUG FIX (2026-09-10, session21e — see
            # dhan_client.is_security_intraday_restricted_error's docstring
            # for the full mechanism). Unlike the time-cutoff branch above,
            # this can fire at ANY time of day — it's a permanent per-
            # security restriction (T2T/ASM/GSM surveillance stocks can
            # never use product_type="INTRADAY"), not a market-close-
            # approaching one. Same treatment: doesn't count toward the
            # generic reject-streak escalation, one throttled alert instead
            # of an alert (or a doomed Dhan resend) every cycle, and the
            # resend-suppression flag is the same _cutoff_key used above —
            # from this service's perspective both are "nothing this
            # service can do about this SAME-DAY exit; wait for tomorrow's
            # CNC sell," they just have different root causes worth
            # explaining differently to a human.
            snap_key = f"intraday_restricted_alert_last_{position.id}"
            last = load_snapshot(db, snap_key) or {}
            last_at_raw = last.get("at")
            due = True
            if last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    elapsed_min = (datetime.now(timezone.utc) - last_at).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            save_snapshot(db, _cutoff_key, {"hit": True})
            if due:
                notify_sync(
                    f"⏰ *EXIT BLOCKED — {position.symbol} can't trade Intraday* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    "Dhan rejected this as a security-level restriction, not "
                    "a timing one: this stock can't use product_type=INTRADAY "
                    "at all (likely a trade-to-trade/surveillance stock), and "
                    "it was bought TODAY so a regular delivery (CNC) sell "
                    "can't go through yet either — CDSL hasn't settled the "
                    "buy. Nothing to do until tomorrow: this stops being a "
                    "same-day trade and the exit goes out as CNC instead "
                    "(needs the usual CDSL 'Verify Holdings' TPIN step). "
                    "This position is skipped — not resent — for the rest "
                    "of today.\n"
                    f"(Further alerts for this position suppressed for "
                    f"{CDSL_ALERT_COOLDOWN_MIN} min.)"
                )
                save_snapshot(db, snap_key, {"at": datetime.now(timezone.utc).isoformat()})
            else:
                logger.info(
                    "Security-intraday-restricted block persists for %s (%s) — "
                    "alert suppressed, still within cooldown.", position.symbol, reason,
                )
        else:
            # BUG FIX (2026-09-07): unlike the invalid-IP and CDSL branches
            # above, this generic branch had no cooldown and no escalation —
            # every cycle that keeps failing for the same reason (a rejection
            # this service doesn't recognize, so it doesn't know NOT to keep
            # retrying) sent an identical Telegram alert forever. Live
            # evidence: PARADEEP's SELL was rejected 5 times in ~6 minutes,
            # each retry producing its own alert with nothing distinguishing
            # "first time seeing this" from "still stuck, same as last time."
            # Apply the same streak-cooldown idiom already proven for CDSL:
            # count consecutive failures for this position, throttle repeat
            # alerts, and once the streak crosses a threshold, escalate to a
            # visibly different message so a human knows this one needs
            # manual attention rather than more silent auto-retries — the
            # position itself is deliberately left untouched either way
            # (still OPEN, still retried next cycle) since we don't know
            # this is unrecoverable the way CDSL's error is.
            snap_key = f"exit_reject_streak_{position.id}"
            streak_state = load_snapshot(db, snap_key) or {}
            streak = int(streak_state.get("count", 0)) + 1
            last_alert_raw = streak_state.get("last_alert_at")
            due = True
            if last_alert_raw:
                try:
                    last_alert_at = datetime.fromisoformat(last_alert_raw)
                    elapsed_min = (datetime.now(timezone.utc) - last_alert_at).total_seconds() / 60.0
                    due = elapsed_min >= CDSL_ALERT_COOLDOWN_MIN
                except Exception:
                    due = True
            if streak >= EXIT_REJECT_STREAK_ESCALATE_AT and not due and streak_state.get("escalated"):
                # Already escalated and still within cooldown — stay silent,
                # just keep the streak count moving.
                logger.info(
                    "exit SELL for %s still rejected (streak=%d, %s) — "
                    "escalation alert suppressed, within cooldown.",
                    position.symbol, streak, reason,
                )
            elif streak >= EXIT_REJECT_STREAK_ESCALATE_AT:
                notify_sync(
                    f"🆘 *EXIT STUCK — {streak} consecutive rejections* — "
                    f"{position.symbol} ×{qty} ({reason})\n"
                    f"Last error: {str(e)[:300]}\n"
                    "This isn't a recognized transient failure (not IP/CDSL) — "
                    "auto-retry alone is unlikely to fix it. Position remains "
                    "OPEN and exposed. Please check Dhan directly (order book, "
                    "actual held quantity for this symbol) before the next "
                    "retry cycle.\n"
                    f"(Further alerts for this position suppressed for "
                    f"{CDSL_ALERT_COOLDOWN_MIN} min — retries continue silently.)"
                )
                streak_state["escalated"] = True
                streak_state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
            elif due:
                notify_sync(
                    f"⚠️ *SELL rejected by Dhan* — {position.symbol} ×{qty} ({reason})\n"
                    f"{str(e)[:300]}"
                )
                streak_state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
            streak_state["count"] = streak
            save_snapshot(db, snap_key, streak_state)
        return False


async def evaluate_mode(db: Session, mode: str) -> dict:
    """One evaluation cycle for every open position in `mode`.
    Checks in order: emergency_gap, stop_hit, target_hit, time_stop,
    breakeven_stop, trail_stop, hold.
    Returns a tally dict for logs and dashboard."""
    positions = open_positions(db, mode)
    if not positions:
        return {
            "evaluated": 0, "held": 0, "trailed": 0,
            "partial_exits": 0, "full_exits": 0,
            "time_stops": 0, "emergency_exits": 0,
        }

    symbols = list({p.symbol for p in positions})
    ticks   = await get_quotes(symbols)

    # Mark-to-market all DEMO positions even on cycles where we don't act —
    # the dashboard should always show current unrealized P&L.
    if mode == "DEMO":
        refresh_unrealized(db, mode, ticks)

    held = trailed = partial_exits = full_exits = time_stops = emergency_exits = 0
    now  = datetime.now(timezone.utc)

    for idx, position in enumerate(positions):
        try:
            import pipeline_status as pstat
            pstat.set_symbol_progress(mode, position.symbol, idx, len(positions))
        except Exception:
            pass

        tick = ticks.get(position.symbol)
        if tick is None:
            _write_exit_decision(
                db, position, "HOLD",
                "No current price available this cycle — skipping evaluation.", 0.0,
            )
            held += 1
            continue

        ltp = tick.price

        # REAL: if a SELL is already in-flight, don't re-evaluate until
        # reconcile confirms or rejects it. Prevents double-selling.
        if mode == "REAL" and _has_pending_real_sell(db, position.symbol):
            _write_exit_decision(
                db, position, "HOLD",
                "Exit already sent to Dhan — awaiting fill confirmation.", ltp,
            )
            held += 1
            continue

        held_days = (now - as_aware(position.opened_at)).days

        # Short-Term Trading Upgrade (2026-09-02): load per-position exit
        # profile. For watchlist-sourced positions this uses the catalyst's
        # horizon_class; for manual/pre-upgrade positions it returns the
        # existing global constants — zero behavior change for those.
        _prof = _load_profile(db, position)
        _trail_schedule  = _prof["trail_atr_schedule"]
        _be_trigger      = _prof["breakeven_atr_trigger"]
        _max_hold        = _prof["max_hold_days"]
        _early_warn      = _prof["early_warn_days"]
        _partial_frac    = _prof["partial_exit_fraction"]
        _horizon         = _prof["horizon_class"]  # for audit trail

        # ── 0. Emergency gap-down exit ────────────────────────────────────────
        # In a weak market (Aug-2026), gap-downs are common. If unrealized loss
        # exceeds EMERGENCY_LOSS_MULT × original stop distance, the stop has
        # been gapped through — exit immediately regardless of current_stop level.
        #
        # 2026-09-01 fix: use position.initial_stop_distance (fixed once at
        # OPEN time) instead of re-deriving from current_stop every cycle.
        # current_stop moves via breakeven/ATR-trail, so the old approach
        # drifted: once trail tightens near LTP the threshold shrinks toward
        # zero (relabels an ordinary stop-hit as "EMERGENCY" — harmless but
        # confusing in the audit log), and once breakeven pushes current_stop
        # above entry the threshold GROWS (delays the emergency catch exactly
        # when there's the most unrealized profit at stake — the opposite of
        # the intent). Rows opened before this migration have no stored
        # value, so they fall back to the previous approximation.
        original_risk = position.initial_stop_distance
        if original_risk is None:
            original_risk = abs(
                position.avg_entry_price - (position.current_stop or position.avg_entry_price)
            )
        unrealized_loss_per_share = position.avg_entry_price - ltp
        if (
            original_risk > 0
            and unrealized_loss_per_share > EMERGENCY_LOSS_MULT * original_risk
        ):
            reasoning = (
                f"EMERGENCY: price ₹{ltp:.2f} gapped {unrealized_loss_per_share:.2f} "
                f"below entry ₹{position.avg_entry_price:.2f} "
                f"({EMERGENCY_LOSS_MULT}× original stop distance ₹{original_risk:.2f}). "
                "Gap-down scenario — closing immediately to prevent further damage."
            )
            _write_exit_decision(db, position, "EMERGENCY_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "emergency_gap_down")
                emergency_exits += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "emergency_gap_down"):
                    emergency_exits += 1
                else:
                    held += 1
            continue

        # ── 1. Stop hit — capital protection always checked first ─────────────
        if position.current_stop is not None and ltp <= position.current_stop:
            reasoning = (
                f"Stop ₹{position.current_stop:.2f} hit at LTP ₹{ltp:.2f}. "
                f"Closing full position ({position.qty_open} shares)."
            )
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "stop_hit")
                full_exits += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "stop_hit"):
                    full_exits += 1
                else:
                    held += 1
            continue

        # ── 2. First target hit — partial exit (60%) ──────────────────────────
        if (
            position.current_target is not None
            and ltp >= position.current_target
            and position.status == "OPEN"
        ):
            qty_to_close = max(1, int(position.qty_open * _partial_frac))
            pct_locked   = qty_to_close / position.qty_open * 100
            reasoning = (
                f"Target ₹{position.current_target:.2f} hit at LTP ₹{ltp:.2f}. "
                f"Locking in {qty_to_close} shares ({pct_locked:.0f}% of position). "
                f"Remainder trailed — stop moved to breakeven ₹{position.avg_entry_price:.2f}."
            )
            _write_exit_decision(db, position, "PARTIAL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, qty_to_close, "target_hit_partial")
                if position.qty_open > 0:
                    # Raise stop to breakeven on the remainder so the rest
                    # is now a "free trade" — worst case exits at entry price.
                    position.current_stop   = max(
                        position.current_stop or 0, position.avg_entry_price
                    )
                    # Nullify target — remainder is now trailed, not held to a
                    # stale fixed target that could be hit again for a second
                    # unintended partial exit.
                    position.current_target = None
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="PARTIAL_EXIT_TRAIL",
                        detail=(
                            f"Stop raised to breakeven ₹{position.avg_entry_price:.2f}, "
                            "target nullified — remainder now on ATR trail."
                        ),
                    ))
                    db.commit()
                partial_exits += 1
            else:
                if _send_real_sell(
                    db, position, qty_to_close, "target_hit_partial", full=False
                ):
                    partial_exits += 1
                    # FIX: nullify target + raise stop to breakeven for REAL too.
                    # Without this, the next cycle sees ltp >= target again and
                    # fires another partial sell on the already-reduced position.
                    position.current_stop   = max(
                        position.current_stop or 0, position.avg_entry_price
                    )
                    position.current_target = None
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="PARTIAL_EXIT_TRAIL",
                        detail=(
                            f"REAL partial sent to Dhan. Stop raised to breakeven "
                            f"₹{position.avg_entry_price:.2f}, target nullified — "
                            "remainder now on ATR trail."
                        ),
                    ))
                    db.commit()
                else:
                    held += 1
            continue

        # ── 3. Time stop (with early warning at EARLY_WARN_DAYS) ─────────────
        # In a choppy market, a non-performing position after MAX_HOLD_DAYS
        # is tying up capital that could be in outperforming midcaps/PSU banks.
        if held_days >= _max_hold and ltp <= position.avg_entry_price * 1.01:
            reasoning = (
                f"Time-stop: held {held_days} days with no meaningful favorable move "
                f"(LTP ₹{ltp:.2f} vs entry ₹{position.avg_entry_price:.2f}). "
                f"Capital freed for better-performing setups."
                f" [horizon={_horizon or 'manual'}]"
            )
            # BUG FIX (2026-09-01): this was logging action="EMERGENCY_EXIT" —
            # copy-pasted from the gap-down branch above. A time-stop close is
            # a full position exit, not the gap-through emergency case (that
            # branch, and its own "emergency_exits" tally counter, are above
            # and untouched). Using "EMERGENCY_EXIT" here mislabeled every
            # time-stop close in the audit trail/dashboard decision history —
            # reasoning correctly said "Time-stop:..." but the action field
            # said EMERGENCY_EXIT, and the counters (time_stops vs
            # emergency_exits) already disagreed with what got written to
            # TradeExitDecision.action. "FULL_EXIT" matches models.py's
            # documented action taxonomy and the stop-hit branch below, which
            # uses the same label for the same kind of event (full close).
            _write_exit_decision(db, position, "FULL_EXIT", reasoning, ltp)
            if mode == "DEMO":
                close_position(db, position, tick, position.qty_open, "time_stop")
                time_stops += 1
            else:
                if _send_real_sell(db, position, position.qty_open, "time_stop"):
                    time_stops += 1
                else:
                    held += 1
            continue

        # Early warning (no exit — just visibility for the dashboard)
        if held_days == _early_warn and ltp <= position.avg_entry_price:
            _write_exit_decision(
                db, position, "HOLD",
                f"Day {held_days} review: LTP ₹{ltp:.2f} still at/below entry "
                f"₹{position.avg_entry_price:.2f}. "
                f"Time-stop fires in {_max_hold - held_days} more days if no move."
                f" [horizon={_horizon or 'manual'}]",
                ltp,
            )
            held += 1
            continue

        # ── 4. Breakeven stop (once gain ≥ _be_trigger × ATR) ────────────────
        # Creates a free-ride floor: once the trade is meaningfully in profit
        # (defined as 1×ATR gain), we protect that by moving stop to entry.
        # Even if price reverses from here, we exit at breakeven, not a loss.
        if tick.atr and ltp > position.avg_entry_price:
            gain_per_share = ltp - position.avg_entry_price
            if gain_per_share >= _be_trigger * tick.atr:
                be_level = position.avg_entry_price
                if position.current_stop is None or position.current_stop < be_level:
                    old_stop = position.current_stop
                    position.current_stop = be_level
                    db.add(models.TradePositionEvent(
                        position_id=position.id, event_type="BREAKEVEN_STOP",
                        detail=(
                            f"Stop raised to breakeven ₹{be_level:.2f} "
                            f"(was ₹{old_stop}) — gain ₹{gain_per_share:.2f} "
                            f"≥ {_be_trigger}×ATR ₹{tick.atr:.2f}. "
                            f"Trade is now a free ride. [horizon={_horizon or 'manual'}]"
                        ),
                    ))
                    db.commit()
                    _write_exit_decision(
                        db, position, "TRAIL_STOP",
                        f"Breakeven stop set at ₹{be_level:.2f} — "
                        f"gain ₹{gain_per_share:.2f} ≥ {_be_trigger}×ATR. "
                        f"Trade is now risk-free. [horizon={_horizon or 'manual'}]",
                        ltp,
                    )
                    trailed += 1
                    continue

        # ── 5. Age-aware ATR trailing stop ────────────────────────────────────
        # Only trail when price is above entry (never trail a losing position —
        # that would loosen the stop, which is wrong).
        # ATR multiplier tightens as trade ages to protect accumulated profit.
        if tick.atr and ltp > position.avg_entry_price:
            trail_mult   = _trail_atr_mult(held_days, schedule=_trail_schedule)
            raw_atr_pct  = tick.atr / ltp * 100.0
            # §6 — clamp: if today's ATR looks like a corporate-action jump, skip
            # the trail update entirely this cycle rather than using a distorted ATR.
            atr_pct = _clamp_for_atr(raw_atr_pct)
            if atr_pct is None:
                # Corporate-action day — don't trail on bad data, just hold current stop
                _write_exit_decision(
                    db, position, "HOLD",
                    f"ATR clamped (corporate-action suspected, raw {raw_atr_pct:.1f}%) "
                    "— trail skipped this cycle to avoid distorted stop.",
                    ltp,
                )
                held += 1
                continue
            trail_candidate = round(ltp * (1 - (atr_pct * trail_mult) / 100.0), 2)

            # Only ever tighten (ratchet up), never loosen the stop.
            if position.current_stop is None or trail_candidate > position.current_stop:
                old_stop = position.current_stop
                position.current_stop = trail_candidate
                db.add(models.TradePositionEvent(
                    position_id=position.id, event_type="STOP_TRAILED",
                    detail=(
                        f"₹{old_stop} → ₹{trail_candidate} "
                        f"(LTP ₹{ltp}, day {held_days}, {trail_mult}×ATR "
                        f"= {atr_pct * trail_mult:.2f}%)"
                    ),
                ))
                db.commit()
                _write_exit_decision(
                    db, position, "TRAIL_STOP",
                    f"Stop trailed to ₹{trail_candidate:.2f} "
                    f"({trail_mult}×ATR, day {held_days} held).",
                    ltp,
                )
                trailed += 1
                continue

        # ── 6. Hold ───────────────────────────────────────────────────────────
        _write_exit_decision(
            db, position, "HOLD",
            f"No exit condition met at LTP ₹{ltp:.2f}. Monitoring.", ltp,
        )
        held += 1

    db.commit()
    tally = {
        "evaluated":      len(positions),
        "held":           held,
        "trailed":        trailed,
        "partial_exits":  partial_exits,
        "full_exits":     full_exits,
        "time_stops":     time_stops,
        "emergency_exits": emergency_exits,
    }
    log_action(
        db, actor="system", action="EXIT_CYCLE", mode=mode, detail=str(tally)
    )
    return tally
