"""
orders/reconcile.py — Super Order exit reconciliation monitor.

WHY THIS EXISTS (STATUS.md open item #4): a Super Order's TARGET_LEG or
STOP_LOSS_LEG fills entirely on Dhan's side — there is no webhook, and
nothing in this service was watching for it. Without this module, a
ScalpPosition stays "OPEN" in our DB forever after Dhan has already
closed it, capital never gets released back into ScalpCapitalLedger, and
`open_symbols` in main.py's trading loop keeps wrongly excluding a symbol
that's actually flat again.

This module polls `dhan_client.get_super_order_list()` (read-only, no
arm check) once per trading-loop tick and cross-references it against
every locally OPEN scalp position that has a `dhan_super_order_id`.

Dhan's /v2/super/orders response shape (per DhanHQ v2 API docs, confirmed
2026-09-12 — see STATUS.md for the source): each element is the ENTRY_LEG
order (top-level `orderId`, `orderStatus`, `legName="ENTRY_LEG"`,
`averageTradedPrice`, `filledQty`) plus a nested `legDetails` array holding
the STOP_LOSS_LEG and TARGET_LEG dicts (`orderId` same as parent,
`legName`, `orderStatus`, `price`, `remainingQuantity`,
`triggeredQuantity`). Dhan's fill status string is `"TRADED"` (same
vocabulary the frontend's groupDhanOrdersBySymbol already relies on for
plain orders — see components/RealAutoTrade.tsx).

ASSUMPTION FLAGGED FOR LIVE VERIFICATION: Dhan's public sample payloads
don't show an `averageTradedPrice` field on the nested leg dicts (only on
the top-level entry). `_extract_leg_price()` tries several plausible key
names before falling back to the leg's static `price` (its target/stop
trigger price, not necessarily the actual fill price — slightly wrong but
never crashes, and is your signal to compare against Dhan's own contract
note the first time a real exit fires). Recommended: eyeball the first
few real TARGET_HIT/STOP_HIT rows against Dhan's app before trusting the
booked P&L number for anything beyond a sanity check.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

import config

import notifier
from capital import ledger, shared_symbol_lock
from execution import dhan_client
from models import ScalpPosition
from orders import overnight_stop
from tz_utils import IST, as_aware, ist_today_str

logger = logging.getLogger("position-stocks-reconcile")

_FILLED_STATUSES = {"TRADED", "FILLED", "EXECUTED", "COMPLETE"}
_DEAD_ENTRY_STATUSES = {"REJECTED", "CANCELLED"}
# BUG FIX (this session): both EOD squareoff and the new manual-exit
# feature (orders/eod_squareoff.py::close_position_now) flatten a
# position via a plain MARKET SELL + a "<STATUS>_PENDING_RECONCILE"
# placeholder that this module resolves once Dhan confirms the real fill
# — see _reconcile_eod_pending's docstring. Every status=="EOD_SQUAREOFF"
# check in this file now checks membership in this tuple instead, so a
# manually-exited position's real exit price/P&L gets resolved the same
# way, rather than being stuck showing ₹0.0 P&L forever.
#
# BUG FIX (this session): "STAGNATION_EXIT" added. close_position_now()
# now stores exit_reason as the real pos.status instead of hardcoding
# "MANUAL_EXIT" (see its docstring), so a stagnation-closed position needs
# to go through this exact same pending-fill resolution path or it would
# be stuck at its entry_price/₹0.0 placeholder forever, same bug this
# tuple already fixed once for MANUAL_EXIT.
_FLAT_SELL_PENDING_STATUSES = ("EOD_SQUAREOFF", "MANUAL_EXIT", "STAGNATION_EXIT")


def _extract_leg_price(leg: dict, parent_row: dict, own_fallback_price: float = 0.0) -> float:
    """own_fallback_price: this position's OWN target_price or stop_price
    (whichever leg actually fired — passed in by the caller, which already
    knows hit_kind). A Dhan Super Order's TARGET_LEG/STOP_LOSS_LEG fills
    at (or essentially at) that pre-set trigger price by construction, so
    it is a realistic last-resort estimate — unlike the hard 0.0 this
    function used to fall through to.

    BUG FIX (this session): every price field this function tries
    (averageTradedPrice/tradedPrice/avgPrice/avgTradedPrice on the leg,
    the leg's own `price`, then the parent row's averageTradedPrice) can
    plausibly be absent from Dhan's real response — the module docstring
    already flags this as unverified against live payloads. The previous
    fallback was a hard 0.0, which run_exit_reconciliation() then used
    directly as `exit_price` with no sanity check: realized_pnl =
    (0 - entry_price) * quantity records a phantom 100%-loss exit as
    real, permanent data — corrupting the trade ledger, wrongly returning
    far too little capital via release_capital(), and potentially
    tripping the daily-loss kill switch over a data-shape gap that has
    nothing to do with an actual trading loss. own_fallback_price (this
    position's own known, never-null target/stop price) is used instead
    of 0.0 whenever every real field comes back empty."""
    for key in ("averageTradedPrice", "tradedPrice", "avgPrice", "avgTradedPrice"):
        v = leg.get(key)
        if v:
            try:
                price = float(v)
            except (TypeError, ValueError):
                continue
            # DIAGNOSTIC (2026-09-16, additive, no behavior change): this is
            # exactly the "ASSUMPTION FLAGGED FOR LIVE VERIFICATION" case
            # from the module docstring — log which key actually resolved
            # the first time a real fill price is found on a leg, so the
            # assumption gets confirmed/refuted from the container logs the
            # next time a TARGET_LEG/STOP_LOSS_LEG fires live, instead of
            # requiring a manual eyeball-against-Dhan's-app comparison.
            logger.info(
                "reconcile: leg fill price resolved via leg['%s']=%.2f "
                "(confirms this key IS present on Dhan's nested leg dict live)",
                key, price,
            )
            return price
    v = leg.get("price")
    if v:
        try:
            price = float(v)
        except (TypeError, ValueError):
            price = None
        if price is not None:
            logger.info(
                "reconcile: leg fill price NOT found via any averageTradedPrice-"
                "style key — fell back to leg['price']=%.2f (the leg's static "
                "trigger price, not a confirmed fill price; refutes the "
                "averageTradedPrice assumption for this leg shape)",
                price,
            )
            return price
    parent_price = parent_row.get("averageTradedPrice")
    if parent_price:
        try:
            return float(parent_price)
        except (TypeError, ValueError):
            pass
    # Last resort: this position's own target/stop trigger price rather
    # than a hard 0.0 — see docstring above. Both columns are NOT NULL on
    # ScalpPosition, so own_fallback_price is only 0.0 here if the caller
    # explicitly passed 0.0 (it never should).
    if own_fallback_price:
        logger.warning(
            "reconcile: no real fill price found in Dhan's response for this "
            "leg — using this position's own trigger price ₹%.2f as a "
            "low-confidence estimate instead of recording a phantom loss.",
            own_fallback_price,
        )
        return float(own_fallback_price)
    return 0.0


_DEAD_EXIT_STATUSES = {"REJECTED", "CANCELLED"}

# run_exit_reconciliation ticks every ~10s (see main.py's trading loop) —
# without this, an unresolvable legacy row would re-fire notify_critical
# every single tick forever. Process-lifetime dedup only (resets on
# restart): a rare duplicate alert after a redeploy is a far better
# trade-off than a Telegram flood every 10 seconds.
_backfill_unresolved_notified: set[int] = set()


def _backfill_legacy_eod_exit_order_ids(db: Session, eod_pending: list[ScalpPosition]) -> int:
    """SESSION41 FIX (STATUS.md open item #4): positions already sitting in
    EOD_SQUAREOFF with a placeholder P&L from BEFORE session40 added
    dhan_exit_order_id capture have no exit-order id to look up, so
    _reconcile_eod_pending's real-fill lookup below can never resolve them
    — they were being left on the entry-price placeholder forever.

    HARD CONSTRAINT (documented on dhan_client.get_order_list, unchanged
    by this fix): Dhan's plain order-list endpoint only returns the
    CURRENT trading day's orders — there is no broker order-history
    endpoint available to this SDK for prior days. That means this
    function can only ever backfill a legacy row whose EOD square-off
    happened earlier TODAY (e.g. this reconciliation loop itself caught
    the position before dhan_exit_order_id existed, or a genuinely old row
    happens to get re-examined the same day it closed). Rows from a prior
    trading day are permanently unresolvable this way — the best this
    function can do for those is stop silently ignoring them and instead
    say so loudly (once per pass), which is the actual, honestly-scoped
    fix here rather than pretending to reconstruct broker history that
    Dhan doesn't expose.

    Matching heuristic for the same-day case: among today's plain orders,
    find a SELL on this position's own security_id, for exactly this
    position's quantity, not already claimed as another position's
    dhan_exit_order_id this pass — preferring one already FILLED/TRADED.
    This is a best-effort match, not a guaranteed-correct one (two
    same-day EOD SELLs on the same symbol/qty would be ambiguous) — every
    backfilled id is logged at INFO with exactly what matched, so a wrong
    guess is auditable, and the existing broker orderType/price
    cross-check in _reconcile_eod_pending still runs on it afterward as a
    second layer of defense.

    Returns the count of legacy rows successfully backfilled (which the
    caller should now be able to resolve via _reconcile_eod_pending in the
    same pass)."""
    legacy = [p for p in eod_pending if not p.dhan_exit_order_id]
    if not legacy:
        return 0

    try:
        plain_orders = dhan_client.get_order_list(db)
    except Exception as e:
        logger.warning("reconcile: legacy EOD backfill — failed to fetch plain order list: %s", e)
        return 0

    already_claimed = {
        str(p.dhan_exit_order_id) for p in eod_pending if p.dhan_exit_order_id
    }
    backfilled = 0
    for pos in legacy:
        matches = [
            row for row in plain_orders
            if str(row.get("transactionType") or row.get("transaction_type") or "").upper() == "SELL"
            and str(row.get("securityId") or row.get("security_id") or "") == str(pos.dhan_security_id)
            and int(row.get("quantity") or 0) == int(pos.quantity)
            and str(row.get("orderId") or row.get("order_id") or "") not in already_claimed
        ]
        if not matches:
            continue
        # Prefer an already-filled leg if more than one same-day candidate
        # matches; otherwise take the first (matches deterministic order.
        matches.sort(
            key=lambda r: str(r.get("orderStatus") or r.get("order_status") or "").upper()
            not in _FILLED_STATUSES
        )
        chosen = matches[0]
        oid = str(chosen.get("orderId") or chosen.get("order_id") or "")
        if not oid:
            continue
        pos.dhan_exit_order_id = oid
        db.commit()
        already_claimed.add(oid)
        backfilled += 1
        logger.info(
            "reconcile: legacy EOD backfill — matched %s (id=%d, qty=%d) to Dhan "
            "order %s (status=%s) from today's order list; handing off to the "
            "normal real-fill reconciliation pass.",
            pos.symbol, pos.id, pos.quantity, oid,
            chosen.get("orderStatus") or chosen.get("order_status"),
        )

    unresolved = [
        p for p in legacy
        if not p.dhan_exit_order_id and p.id not in _backfill_unresolved_notified
    ]
    if unresolved:
        msg = (
            f"reconcile: legacy EOD backfill — {len(unresolved)} pre-session40 "
            f"EOD_SQUAREOFF position(s) still have no dhan_exit_order_id and "
            f"could not be matched in today's Dhan order list "
            f"({', '.join(f'{p.symbol}(id={p.id})' for p in unresolved[:10])}"
            f"{'...' if len(unresolved) > 10 else ''}). These are from a prior "
            f"trading day — Dhan's order-list endpoint only covers today, so "
            f"they cannot be auto-resolved and will keep using the stale "
            f"entry-price placeholder P&L. Needs manual review against Dhan's "
            f"own trade book / contract notes for the dates in question. "
            f"(This alert fires once per position per service restart.)"
        )
        logger.critical(msg)
        notifier.notify_critical(msg)
        _backfill_unresolved_notified.update(p.id for p in unresolved)

    return backfilled


def _reconcile_eod_pending(db: Session, eod_pending: list[ScalpPosition]) -> int:
    """2026-09-15 fix (session40 — the "future improvement" flagged in
    run_exit_reconciliation's dead-code comment below, now implemented):
    resolves each EOD_SQUAREOFF_PENDING_RECONCILE position's REAL plain
    MARKET SELL fill via dhan_client.get_order_list() +
    pos.dhan_exit_order_id (captured by eod_squareoff.py's
    _fire_flat_sell), instead of leaving exit_price/realized_pnl at the
    entry_price/₹0.0 placeholder forever. Runs BEFORE the super-order pass
    below so a position this resolves never also falls through into that
    pass's own (now-superseded) placeholder-only EOD_SQUAREOFF branch.

    Same defense-in-depth this service's dhan_client.place_order() and
    real-trade-service's reconcile.py already apply: also cross-checks
    Dhan's own reported orderType/price for this SELL against what was
    actually requested (MARKET/0) and logs CRITICAL on a mismatch — as of
    session41 this also fires a Telegram alert via notifier.py (see that
    module), not just a log line.

    Positions with no dhan_exit_order_id (pre-session40 rows, or a case
    where _fire_flat_sell's retries were all exhausted with no order ever
    accepted) are left untouched here for the caller's existing placeholder
    fallback — this function only handles the case it can actually resolve.
    See _backfill_legacy_eod_exit_order_ids below (session41) for a
    best-effort attempt to resolve the pre-session40 rows too, run by the
    caller before this function.
    """
    candidates = [p for p in eod_pending if p.dhan_exit_order_id]
    if not candidates:
        return 0

    try:
        plain_orders = dhan_client.get_order_list(db)
    except Exception as e:
        logger.warning("reconcile: EOD-pending — failed to fetch plain order list: %s", e)
        return 0

    by_id: dict[str, dict] = {}
    for row in plain_orders:
        oid = str(row.get("orderId") or row.get("order_id") or "")
        if oid:
            by_id[oid] = row

    resolved = 0
    for pos in candidates:
        row = by_id.get(str(pos.dhan_exit_order_id))
        if row is None:
            continue  # not visible yet — next pass, keep the placeholder for now

        broker_order_type = str(row.get("orderType") or row.get("order_type") or "").upper()
        broker_price = row.get("price")
        # 2026-09-17 fix: Dhan always echoes a MARKET order back in the
        # order book as a "LIMIT" order with a computed protection price —
        # normal NSE-equity broker behavior, not a sign this flat-close
        # went wrong. This fired on every single EOD flat-sell before this
        # fix. Only alert when the broker's order type is something other
        # than this expected MARKET->LIMIT echo.
        if broker_order_type and broker_order_type not in ("MARKET", "LIMIT"):
            msg = (
                f"reconcile: EOD flat-SELL BROKER ORDER TYPE MISMATCH for "
                f"{pos.symbol} (id={pos.id}, order {pos.dhan_exit_order_id}) — sent "
                f"MARKET but Dhan reports orderType={broker_order_type} "
                f"(price={broker_price}). This position's flat-close is not what "
                f"this service believes it is — investigate immediately."
            )
            logger.critical(msg)
            notifier.notify_critical(msg)

        status = str(row.get("orderStatus") or row.get("order_status") or "").upper()
        if status in _FILLED_STATUSES:
            raw_fill = row.get("averageTradedPrice") or row.get("average_traded_price")
            try:
                real_exit_price = float(raw_fill) if raw_fill else None
            except (TypeError, ValueError):
                real_exit_price = None
            if real_exit_price is None:
                logger.warning(
                    "reconcile: EOD flat-SELL for %s (id=%d, order %s) reports %s but no "
                    "fill price — leaving placeholder for next pass.",
                    pos.symbol, pos.id, pos.dhan_exit_order_id, status,
                )
                continue

            _resolve_pending_with_price(db, pos, real_exit_price, late=False)
            resolved += 1
        elif status in _DEAD_EXIT_STATUSES:
            # The flat SELL itself died with zero fill, even after
            # _fire_flat_sell's bounded retry — this position is still
            # genuinely open at the broker past hard-flat time and cannot
            # be auto-retried again until tomorrow's sweep (this service
            # has no continuous exit-retry loop the way real-trade-
            # service's exit_engine does). Surface it loudly rather than
            # leaving it silently mislabeled EOD_SQUAREOFF with a ₹0.0
            # placeholder P&L that looks like a real, closed, break-even
            # trade.
            dead_status_prefix = pos.status  # EOD_SQUAREOFF or MANUAL_EXIT, before being overwritten below
            pos.status = "ERROR"
            pos.error_message = f"{dead_status_prefix}_SELL_DEAD: order {pos.dhan_exit_order_id} came back {status} with zero fill — position may still be open at the broker, needs manual review."
            db.commit()
            # BUG FIX (audit follow-up): eod_squareoff.py released this
            # position's capital_risked back to available_capital the
            # moment it PLACED the flat SELL, before knowing whether it
            # would fill. It didn't — the position is still genuinely
            # open at the broker with real capital tied up in it, but
            # our ledger already counted that capital as free. Reclaim
            # it so available_capital reflects reality again; see
            # ledger.reclaim_premature_release's docstring.
            ledger.reclaim_premature_release(db, capital_risked=pos.capital_risked)
            # AUDIT FIX (session60): eod_squareoff.py released this
            # symbol's cross-service lock optimistically when it fired the
            # flat SELL, before knowing whether it would fill — mirrors the
            # capital reclaim just above. The SELL died with zero fill, so
            # the position is still genuinely open at the broker; re-claim
            # the lock so a duplicate buy (by either service) can't slip in
            # while this position sits in ERROR awaiting manual review.
            # Best-effort — if another service raced in and already holds
            # it, that's now a real conflict for a human to resolve
            # manually anyway, not something this call could have prevented.
            shared_symbol_lock.try_claim(db, pos.symbol)
            resolved += 1
            msg = (
                f"reconcile: {pos.symbol} (id={pos.id}) EOD flat-SELL order "
                f"{pos.dhan_exit_order_id} came back {status} with ZERO fill — "
                f"position marked ERROR, likely still open at the broker past "
                f"hard-flat time. Needs manual review."
            )
            logger.critical(msg)
            notifier.notify_critical(msg)
    return resolved


# Back-compat alias — the status vocabulary now lives in orders/overnight_stop.py.
_OVERNIGHT_STOP_PARTIAL_STATUSES = overnight_stop.PARTIAL_STATUSES


def _resolve_pending_with_price(db: Session, pos: ScalpPosition, real_exit_price: float, *, late: bool) -> None:
    """Books the real exit of a *_PENDING_RECONCILE position (shared by the
    same-day order-list path and the prior-day trade-history path).

    session72: a position that had overnight-stop PARTIALS booked before its
    flat SELL keeps that earlier realized P&L — the SELL's own P&L is added to
    it instead of overwriting it, and the % uses the full quantity ever held.
    The ledger only ever receives THIS sell's P&L (partials were released as
    they happened). late=True (prior-day resolution) books to the ledger's
    lifetime total only, never to today's daily-loss counter."""
    booked_qty = int(pos.overnight_stop_prior_qty or 0) + int(pos.overnight_stop_filled_qty_so_far or 0)
    prior_pnl = (pos.realized_pnl or 0.0) if booked_qty > 0 else 0.0
    sell_pnl = (real_exit_price - pos.entry_price) * pos.quantity
    total_pnl = prior_pnl + sell_pnl
    basis = pos.entry_price * (pos.quantity + booked_qty)
    total_pct = (total_pnl / basis * 100.0) if basis else 0.0

    pos.exit_price = real_exit_price
    pos.realized_pnl = total_pnl
    pos.realized_pnl_pct = total_pct
    pos.error_message = None
    db.commit()

    if late:
        ledger.book_late_realized_pnl(db, sell_pnl)
    else:
        # The placeholder release already returned capital_risked with P&L 0.0;
        # book the real P&L now (also re-runs the daily-loss kill-switch check).
        ledger.release_capital(db, position_value=0.0, realized_pnl=sell_pnl)
    logger.info("reconcile: %s (id=%d) %s — real fill resolved @ ₹%.2f, P&L ₹%.2f (%.2f%%)%s",
                pos.symbol, pos.id, pos.status, real_exit_price, total_pnl, total_pct,
                " [late, resolved from trade history]" if late else "")
    notifier.notify_sync(
        f"✅ <b>{pos.status} — real fill resolved</b> — {pos.symbol} x{pos.quantity}\n"
        f"Exit ₹{real_exit_price:.2f} | P&L ₹{total_pnl:,.2f} ({total_pct:.2f}%)"
        + (" (resolved late from Dhan trade history)" if late else "")
    )


_last_stuck_sweep_ts = 0.0
_stuck_alerted: set[int] = set()


def _ist_date_str(dt) -> str:
    return as_aware(dt).astimezone(IST).strftime("%Y-%m-%d")


def list_pending_reconcile(db: Session) -> list[dict]:
    """Read-only diagnostic (GET /reconcile/pending): every row still carrying a
    *_PENDING_RECONCILE sentinel, with its age and what it is waiting on."""
    rows = (db.query(ScalpPosition)
            .filter(ScalpPosition.error_message.like("%_PENDING_RECONCILE%"))
            .order_by(ScalpPosition.id.desc()).all())
    today = ist_today_str()
    out = []
    for p in rows:
        closed_day = _ist_date_str(p.closed_at) if p.closed_at else None
        age_days = (date.fromisoformat(today) - date.fromisoformat(closed_day)).days if closed_day else None
        out.append({
            "id": p.id, "symbol": p.symbol, "status": p.status, "closed_day_ist": closed_day,
            "age_days": age_days, "dhan_exit_order_id": p.dhan_exit_order_id,
            "entry_price": p.entry_price, "exit_price": p.exit_price, "realized_pnl": p.realized_pnl,
            "resolvable_via": ("order_list (same day)" if age_days == 0 else "trade_history (prior day)"),
            "error_message": p.error_message,
        })
    return out


def resolve_stuck_pending(db: Session, *, force: bool = False) -> dict:
    """Session 72, open-issue #5 (RML): resolves PRIOR-DAY *_PENDING_RECONCILE
    rows the same-day order-list path can never see (Dhan's order book only
    holds today). Uses Dhan's trade history (dhanhq.get_trade_history).

    Per row: (1) self-heal an inconsistent row that already has a real exit
    price + P&L but kept the sentinel; (2) resolve by dhan_exit_order_id from
    trade history — only when the traded qty covers the position (never guess
    on a partial); (3) with no order id, adopt one ONLY if exactly one SELL
    order for this security that day matches the quantity; (4) after
    PENDING_RECONCILE_MAX_AGE_DAYS still unresolved -> rewrite the sentinel to
    *_UNRESOLVED and alert once, so nothing sits 'pending' forever. Throttled;
    force=True (POST /reconcile/pending/resolve) bypasses the throttle."""
    global _last_stuck_sweep_ts
    summary = {"examined": 0, "resolved": 0, "self_healed": 0, "aged_out": 0, "still_pending": 0}
    now_m = time.monotonic()
    if not force and now_m - _last_stuck_sweep_ts < config.PENDING_RECONCILE_SWEEP_INTERVAL_S:
        summary["skipped"] = "throttled"
        return summary
    _last_stuck_sweep_ts = now_m

    today = ist_today_str()
    # BUG FIX (session 75, live evidence — INDORAMA/NAHARINDUS stuck 2-3 days):
    # this used to also require status IN _FLAT_SELL_PENDING_STATUSES
    # ("EOD_SQUAREOFF"/"MANUAL_EXIT"/"STAGNATION_EXIT"). But a row that
    # started down that placeholder path (sentinel set, status EOD_SQUAREOFF)
    # can later have its status overwritten to TARGET_HIT/STOP_HIT/CLOSED by
    # an unrelated close path (e.g. a race between the EOD-flatten sweep and
    # the overnight-stop/intraday-exit reconcile loop) without that path ever
    # knowing to clear a sentinel it didn't set. list_pending_reconcile() (the
    # GET /reconcile/pending diagnostic) has always filtered on the sentinel
    # text alone, with no status restriction — this resolution sweep silently
    # disagreed, so any row whose status drifted outside those 3 values was
    # permanently invisible to self-heal/trade-history-resolve/age-out, and
    # sat "pending" forever with no path to ever clear it. The self-heal case
    # below (real exit_price/realized_pnl already present) and the
    # trade-history match don't depend on status at all, so dropping the
    # status filter here — matching list_pending_reconcile() exactly — is
    # safe and closes the actual gap the diagnostic was built to catch.
    rows = (db.query(ScalpPosition)
            .filter(ScalpPosition.error_message.like("%_PENDING_RECONCILE%")).all())
    stuck = [p for p in rows if p.closed_at is None or _ist_date_str(p.closed_at) < today]
    if not stuck:
        return summary

    earliest = min((_ist_date_str(p.closed_at) if p.closed_at else today) for p in stuck)
    from_d = (date.fromisoformat(earliest) - timedelta(days=1)).isoformat()
    trades: list = []
    trades_ok = True
    try:
        trades = dhan_client.get_trade_history(db, from_d, today)
    except Exception as e:
        trades_ok = False
        logger.warning("reconcile: stuck-pending sweep — trade-history fetch failed (%s); will retry", e)

    for pos in stuck:
        summary["examined"] += 1
        try:
            if (pos.exit_price and pos.entry_price and abs(pos.exit_price - pos.entry_price) > 1e-9
                    and abs(pos.realized_pnl or 0.0) > 1e-9):
                pos.error_message = None
                db.commit()
                summary["self_healed"] += 1
                continue
            if not trades_ok:
                summary["still_pending"] += 1
                continue

            oid = pos.dhan_exit_order_id
            day = _ist_date_str(pos.closed_at) if pos.closed_at else None
            if not oid:
                per_order: dict[str, int] = {}
                for t in trades:
                    if str(t.get("transactionType") or "").upper() != "SELL":
                        continue
                    if str(t.get("securityId") or "") != str(pos.dhan_security_id):
                        continue
                    stamp = str(t.get("createTime") or t.get("exchangeTime") or t.get("updateTime") or "")[:10]
                    if day and stamp and stamp != day:
                        continue
                    o = overnight_stop._trade_order_id(t)
                    if o:
                        per_order[o] = per_order.get(o, 0) + overnight_stop._trade_qty(t)
                cands = [o for o, q in per_order.items() if q == pos.quantity]
                if len(cands) == 1:
                    oid = cands[0]
                    pos.dhan_exit_order_id = oid
                    db.commit()

            qty, avg = overnight_stop.aggregate_trades(trades, oid) if oid else (0, None)
            if avg and qty >= pos.quantity:
                _resolve_pending_with_price(db, pos, avg, late=True)
                summary["resolved"] += 1
                continue

            age_days = (date.fromisoformat(today) - date.fromisoformat(day)).days if day else config.PENDING_RECONCILE_MAX_AGE_DAYS
            if age_days >= config.PENDING_RECONCILE_MAX_AGE_DAYS:
                pos.error_message = (
                    f"{pos.status}_UNRESOLVED: exit fill price could not be recovered from Dhan "
                    f"(order id {oid or 'unknown'}, traded qty {qty}/{pos.quantity}); exit_price/"
                    f"realized_pnl are the entry-price placeholder — check Dhan's contract note."
                )
                db.commit()
                summary["aged_out"] += 1
                if pos.id not in _stuck_alerted:
                    _stuck_alerted.add(pos.id)
                    notifier.notify_critical(
                        f"⚠️ <b>{pos.status} exit price UNRESOLVED</b> — {pos.symbol} (id={pos.id}) closed "
                        f"{day}: {age_days}d old, Dhan trade history has no matching SELL "
                        f"(order {oid or 'unknown'}). P&L shown is a placeholder."
                    )
            else:
                summary["still_pending"] += 1
        except Exception as e:
            db.rollback()
            logger.error("reconcile: stuck-pending sweep failed for %s (id=%s): %s", pos.symbol, pos.id, e, exc_info=True)
    if summary["resolved"] or summary["aged_out"] or summary["self_healed"]:
        logger.info("reconcile: stuck-pending sweep %s", summary)
    return summary


def _reconcile_overnight_stops(db: Session) -> int:
    """Closes out overnight-carried positions once their protective
    STOP_LOSS_MARKET fills, booking partials/expired-partials exactly once.
    All accounting lives in orders/overnight_stop.py (session 72); this is
    just the fast-loop driver. Runs BEFORE the super-order pass because a
    carried position's dhan_super_order_id was nulled at CNC conversion."""
    positions = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status == "OPEN",
            ScalpPosition.overnight_converted_to_cnc == True,  # noqa: E712 - avoid .is_() on Oracle NUMBER(1) boolean (emits invalid "IS 1")
            ScalpPosition.overnight_stop_order_id.isnot(None),
        )
        .all()
    )
    if not positions:
        return 0
    try:
        plain_orders = dhan_client.get_order_list(db)
    except Exception as e:
        logger.warning("reconcile: overnight-stop check — failed to fetch order list: %s", e)
        return 0
    by_id: dict[str, dict] = {}
    for row in plain_orders:
        oid = str(row.get("orderId") or row.get("order_id") or "")
        if oid:
            by_id[oid] = row
    closed = 0
    for pos in positions:
        row = by_id.get(str(pos.overnight_stop_order_id))
        if row is None:
            continue  # aged out of today's book — main.py's morning recheck uses trade history
        try:
            if overnight_stop.settle_stop_row(db, pos, row)["closed"]:
                closed += 1
        except Exception as e:
            db.rollback()
            logger.error("reconcile: overnight-stop settle failed for %s (id=%s): %s", pos.symbol, pos.id, e, exc_info=True)
    return closed


def run_exit_reconciliation(db: Session) -> int:
    """Check every locally-OPEN scalp position against Dhan's live super
    order book. Closes any position whose TARGET_LEG or STOP_LOSS_LEG has
    filled (or whose ENTRY_LEG was rejected/cancelled before ever filling),
    releases its reserved capital + realized P&L back into the ledger, and
    returns the count of positions closed this pass.

    AUDIT FIX (2026-09-19): also runs _reconcile_overnight_stops() first —
    see that function's docstring for why overnight-carried CNC positions
    (protected by a plain STOP_LOSS_MARKET order, not a super order) need
    their own detection path entirely separate from the super-order logic
    below, which never sees them (dhan_super_order_id is nulled at CNC
    conversion time).
    """
    overnight_closed = _reconcile_overnight_stops(db)
    try:
        resolve_stuck_pending(db)
    except Exception as e:
        db.rollback()
        logger.error("reconcile: stuck-pending sweep error: %s", e, exc_info=True)

    # AUDIT FIX: also pick up EOD_SQUAREOFF positions whose exit_price is
    # still the entry_price placeholder (recorded by eod_squareoff.py's
    # `pos.error_message = "EOD_SQUAREOFF_PENDING_RECONCILE..."` comment).
    # Without this, EOD-squared-off positions would show P&L=₹0.0 forever
    # in /positions and /trades/history even after the MARKET SELL filled on
    # Dhan's side — the real fill price and P&L would never be populated.
    # We detect these by the status (EOD_SQUAREOFF) and the placeholder
    # sentinel in error_message rather than a separate column, so no schema
    # change is needed.
    # 2026-09-21 fix (session80 — user-reported sync gap, REFEX/LLOYDSENT):
    # this used to be `status == "OPEN"` only. That's the actual bug behind
    # "Dhan already shows these closed, Stockky still shows them open with
    # EXIT_LEGS_REJECTED" — see the dead-leg-detection fix a bit further
    # down in this same function for the full incident. In short: a
    # position gets marked EXIT_LEGS_REJECTED the moment EITHER exit leg
    # (TARGET_LEG or STOP_LOSS_LEG) is REJECTED/CANCELLED/EXPIRED, even
    # when the OTHER leg is still perfectly live and resting at the broker.
    # Once marked, this query excluded it from every future pass — so the
    # surviving leg could fill on Dhan's side (exactly what happened here:
    # both stocks sold at ~their STOP_LOSS_LEG price) and this service
    # would never find out: no P&L booked, no capital released, no
    # notification, and the dashboard kept showing a real Dhan holding
    # that had actually already been sold. Including EXIT_LEGS_REJECTED
    # rows here (still gated on dhan_super_order_id.isnot(None) — no
    # super order id means nothing to poll) lets the exact same fill-
    # detection code below pick up a surviving leg's fill for these too,
    # instead of waiting on EOD squareoff's blind plain-MARKET-SELL retry
    # (which would now just get rejected by Dhan anyway, since the
    # position is already flat there).
    open_positions = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")),
            ScalpPosition.dhan_super_order_id.isnot(None),
        )
        .all()
    )
    # BUG FIX (this session — added the "manual exit" feature): a manual
    # close (orders/eod_squareoff.py::close_position_now) uses the exact
    # same placeholder-then-reconcile pattern as EOD squareoff (plain
    # MARKET SELL, exit_price=entry_price placeholder, a
    # "<STATUS>_PENDING_RECONCILE" sentinel in error_message) but under
    # status="MANUAL_EXIT" instead of "EOD_SQUAREOFF" — this query and
    # every other status=="EOD_SQUAREOFF" check below it in this function
    # is generalized to _FLAT_SELL_PENDING_STATUSES so a manually-exited
    # position's real fill price/P&L gets resolved the same way an
    # EOD-squared-off one always has, instead of being stuck on the
    # ₹0.0-P&L placeholder forever.
    # AUDIT FIX (2026-09-19, overnight-stop reconcile gap): this used to
    # also require dhan_super_order_id.isnot(None). That's correct for a
    # normal same-day position (always has one), but orders/eod_squareoff.py
    # explicitly NULLs dhan_super_order_id the moment a position converts
    # INTRADAY -> CNC for an overnight carry (the bracket legs are gone).
    # So an overnight-carried position that gets flat-SOLD later — via the
    # next day's EOD sweep, a manual exit, or the stop_failed_positions
    # fallback — used to be silently excluded from this whole resolution
    # path even though _reconcile_eod_pending() below only ever keys off
    # dhan_exit_order_id and never needed dhan_super_order_id at all. Such
    # a position stayed on its ₹0.0 entry_price-placeholder P&L forever.
    # Dropping the dhan_super_order_id requirement here; the fallback pass
    # further down (which DOES need dhan_super_order_id to read the
    # original entry leg) simply won't apply to these rows, which is fine
    # since their entry already filled long ago — that's how they qualified
    # to carry overnight in the first place.
    eod_pending = (
        db.query(ScalpPosition)
        .filter(
            ScalpPosition.status.in_(_FLAT_SELL_PENDING_STATUSES),
            ScalpPosition.error_message.like("%_PENDING_RECONCILE%"),
        )
        .all()
    )
    # SESSION41 FIX: before resolving real fills, best-effort backfill
    # dhan_exit_order_id on any pre-session40 legacy row that doesn't have
    # one yet — see _backfill_legacy_eod_exit_order_ids' docstring for the
    # matching heuristic and its documented same-day-only limitation. Runs
    # BEFORE _reconcile_eod_pending so a row it successfully backfills is
    # picked up by the real-fill lookup in this same pass, not next cycle.
    _backfill_legacy_eod_exit_order_ids(db, eod_pending)

    # 2026-09-15 fix (session40): resolve EOD-pending positions' REAL flat-
    # SELL fill first, via get_order_list()/dhan_exit_order_id — see
    # _reconcile_eod_pending's docstring. Positions it resolves (real fill
    # booked, or marked ERROR on a dead zero-fill SELL) are dropped from
    # eod_pending below so the legacy placeholder-only branch further down
    # (which only ever looks at the ORIGINAL super order's ENTRY_LEG, never
    # the actual exit) doesn't reprocess and re-log them as still-placeholder.
    _reconcile_eod_pending(db, eod_pending)
    eod_pending = [
        p for p in eod_pending
        if p.status in _FLAT_SELL_PENDING_STATUSES
        and (p.error_message or "").startswith(f"{p.status}_PENDING_RECONCILE")
    ]

    all_positions = open_positions + eod_pending
    if not all_positions:
        # AUDIT FIX: must still return overnight_closed here — previously
        # this early-return path (which normal ticks hit most of the time,
        # since super-order positions requiring this loop aren't always
        # present) would silently drop a same-pass overnight-stop closure
        # from the reported count, even though it was already committed.
        return overnight_closed
    # Alias for the rest of the function (which iterates `open_positions`)
    open_positions = all_positions

    try:
        super_orders = dhan_client.get_super_order_list(db)
    except Exception as e:
        logger.error("reconcile: failed to fetch super order list: %s", e)
        # AUDIT FIX: same as above — don't drop an already-committed
        # overnight-stop closure just because this unrelated fetch failed.
        return overnight_closed

    by_id: dict[str, dict] = {}
    for row in super_orders:
        oid = str(row.get("orderId") or "")
        if oid:
            by_id[oid] = row

    closed = 0
    for pos in open_positions:
        row = by_id.get(str(pos.dhan_super_order_id))
        if row is None:
            # Not (yet) visible in today's order book — could be a brief
            # timing gap right after placement. Skip silently; next tick
            # will pick it up.
            continue

        # AUDIT FIX (this session — "check buy/sell for every scenario,
        # consider very high and frequent price change"): entry_price was
        # set exactly once, in orders/entry.py, to the pre-order LTP
        # sampled at scan/decision time — and never corrected afterwards.
        # Every realized_pnl / realized_pnl_pct this function computes
        # below is (exit_price - pos.entry_price) * quantity, so a stale
        # entry reference silently mis-states booked P&L. On a genuinely
        # calm stock this barely matters; on the fast-moving, volatile
        # names this scalp strategy specifically targets, the real
        # ENTRY_LEG's average traded price (`row["averageTradedPrice"]`,
        # already being read a few lines below via `_extract_leg_price`'s
        # parent-row fallback for the EXIT side — the entry side just
        # never used it) can differ meaningfully from that LTP snapshot:
        # queueing/network latency between the scan tick and Dhan
        # receiving the MARKET order, plus the order's own market impact
        # on a thin/fast-moving name. Correcting it here, as soon as
        # Dhan's response confirms a real fill, before it's ever used in a
        # P&L calc — idempotent (only writes when the value actually
        # changed) and applies to both still-OPEN positions and
        # EOD_SQUAREOFF-pending ones (whose exit_price is a placeholder
        # copy of entry_price — see eod_squareoff.py — so it's bumped in
        # lockstep to keep that placeholder's phantom P&L at exactly zero,
        # same as before, just anchored to the real fill instead of the
        # estimate). Does NOT touch capital_risked/the ledger — that's a
        # separate, already-reserved software allocation and out of this
        # fix's scope.
        if pos.status in ("OPEN",) + _FLAT_SELL_PENDING_STATUSES:
            entry_status_now = str(row.get("orderStatus", "")).upper()
            if entry_status_now in _FILLED_STATUSES:
                raw_fill = row.get("averageTradedPrice")
                real_entry_price: Optional[float] = None
                if raw_fill:
                    try:
                        real_entry_price = float(raw_fill)
                    except (TypeError, ValueError):
                        real_entry_price = None
                if real_entry_price and abs(real_entry_price - pos.entry_price) > 1e-6:
                    old_entry_price = pos.entry_price
                    pos.entry_price = real_entry_price
                    if pos.status in _FLAT_SELL_PENDING_STATUSES and pos.exit_price == old_entry_price:
                        pos.exit_price = real_entry_price

                    # AUDIT FIX (this session): the previously-flagged
                    # follow-up — capital_risked was reserved off the same
                    # stale pre-order LTP estimate as entry_price, and had
                    # the same "never corrected" gap. Now that the real
                    # fill price is known, recompute the real cost and
                    # push the delta through the ledger so
                    # available_capital stays consistent with what this
                    # position will actually return at exit (see
                    # capital/ledger.py::reconcile_position_cost's
                    # docstring for the full reasoning, including why a
                    # positive delta is allowed to push available_capital
                    # negative rather than being silently clamped).
                    old_capital_risked = pos.capital_risked
                    real_capital_cost = pos.quantity * real_entry_price
                    delta = real_capital_cost - old_capital_risked
                    pos.capital_risked = real_capital_cost
                    db.commit()
                    if delta != 0:
                        ledger.reconcile_position_cost(db, delta=delta)

                    logger.info(
                        "reconcile: %s (id=%d) entry_price corrected ₹%.2f -> ₹%.2f, "
                        "capital_risked ₹%.2f -> ₹%.2f (Dhan's real avg fill vs "
                        "pre-order LTP estimate)",
                        pos.symbol, pos.id, old_entry_price, real_entry_price,
                        old_capital_risked, real_capital_cost,
                    )

                    # 2026-09-18 (user audit finding): the correction above
                    # fixed the REPORTED entry_price/P&L, but the live
                    # TARGET_LEG/STOP_LOSS_LEG prices already sent to Dhan
                    # were computed off the pre-fill LTP and were never
                    # re-submitted — so the actual R:R being executed on
                    # the exchange could drift slightly from the intended
                    # adaptive_target_pct/adaptive_stop_pct on the
                    # noisiest names. Only meaningful for still-OPEN
                    # positions (a FLAT_SELL_PENDING one already has its
                    # legs cancelled or is on its way out — re-arming
                    # them would be pointless and could race the close).
                    if pos.status == "OPEN" and config.USE_SUPER_ORDER and pos.dhan_super_order_id:
                        try:
                            new_target = round(real_entry_price * (1 + pos.adaptive_target_pct / 100.0), 2)
                            new_stop = round(real_entry_price * (1 - pos.adaptive_stop_pct / 100.0), 2)
                            dhan_client.modify_super_order(
                                db, order_id=pos.dhan_super_order_id,
                                order_leg="TARGET_LEG", target_price=new_target,
                            )
                            dhan_client.modify_super_order(
                                db, order_id=pos.dhan_super_order_id,
                                order_leg="STOP_LOSS_LEG", stop_loss_price=new_stop,
                            )
                            old_target, old_stop = pos.target_price, pos.stop_price
                            pos.target_price = new_target
                            pos.stop_price = new_stop
                            db.commit()
                            logger.info(
                                "reconcile: %s (id=%d) re-armed target/stop legs for the "
                                "corrected fill price: target ₹%.2f -> ₹%.2f, stop ₹%.2f -> ₹%.2f",
                                pos.symbol, pos.id, old_target, new_target, old_stop, new_stop,
                            )
                        except Exception as e:
                            # Not fatal — the legs stay at their original
                            # (slightly-off-fill) prices, which is exactly
                            # today's pre-fix behavior, not a regression.
                            # Common benign cause: the leg already filled
                            # or was cancelled between the entry-price
                            # correction above and this modify attempt.
                            logger.warning(
                                "reconcile: %s (id=%d) failed to re-arm target/stop legs "
                                "after fill-price correction (%s) — legs remain at their "
                                "original prices",
                                pos.symbol, pos.id, e,
                            )

        leg_details = row.get("legDetails") or []
        target_leg = next((l for l in leg_details if l.get("legName") == "TARGET_LEG"), None)
        stop_leg = next((l for l in leg_details if l.get("legName") == "STOP_LOSS_LEG"), None)

        hit_leg: Optional[dict] = None
        hit_kind: Optional[str] = None
        if target_leg and str(target_leg.get("orderStatus", "")).upper() in _FILLED_STATUSES:
            hit_leg, hit_kind = target_leg, "TARGET_HIT"
        elif stop_leg and str(stop_leg.get("orderStatus", "")).upper() in _FILLED_STATUSES:
            hit_leg, hit_kind = stop_leg, "STOP_HIT"

        # 2026-09-15 fix (session41b — DATAMATICS/ZENSARTECH rejection storm):
        # When BOTH exit legs (TARGET_LEG and STOP_LOSS_LEG) are REJECTED by
        # Dhan — circuit-limit, surveillance restriction, or similar — the
        # super order can never self-exit.  Previously this was invisible:
        # hit_kind stayed None, the position stayed OPEN, and EOD squareoff
        # fired 60+ identical MARKET SELL orders that also rejected (same
        # underlying cause) producing the spam we saw in the live order book.
        #
        # Detection: if neither leg filled AND every exit leg that actually
        # exists on this order is REJECTED/CANCELLED/EXPIRED, mark the
        # position as EXIT_LEGS_REJECTED so:
        #   a) EOD squareoff knows to attempt a plain MARKET SELL once (not
        #      loop forever) and then give up gracefully.
        #   b) The dashboard shows the real state instead of "OPEN" forever.
        #
        # 2026-09-21 fix (session80 — user-reported sync gap, REFEX/
        # LLOYDSENT): this used to fire on `target_dead or stop_dead` — ONE
        # dead leg was enough to mark the whole position EXIT_LEGS_REJECTED,
        # even when the OTHER leg was still perfectly live at the broker.
        # For both REFEX and LLOYDSENT, only the TARGET_LEG had been
        # rejected; the STOP_LOSS_LEG was still resting normally and later
        # filled for real on Dhan (both closed at ~their stop price) — but
        # because this code had already flipped them to EXIT_LEGS_REJECTED,
        # the open_positions query above (pre-fix) stopped polling their
        # super order entirely, so that real fill was never seen: no exit
        # price, no P&L, no capital released, position still shown "open"
        # with a stale EXIT_LEGS_REJECTED tag days after Dhan had already
        # sold it. Now requires EVERY exit leg that exists on the order to
        # be dead — a single-surviving-leg position is left as-is (still
        # OPEN, still being polled) so its live leg's eventual fill is
        # caught by the normal hit_kind detection just above, exactly like
        # any other open position. Only a position with NO live exit leg
        # left at all is genuinely unable to self-exit and gets tagged.
        #
        # We only do this for positions in status OPEN (not EOD_SQUAREOFF /
        # already-handled paths above) to avoid double-processing.
        if hit_kind is None and pos.status == "OPEN":
            _DEAD_LEG_STATUSES = ("REJECTED", "CANCELLED", "EXPIRED")
            existing_legs = [l for l in (target_leg, stop_leg) if l is not None]
            dead_legs = [
                l for l in existing_legs
                if str(l.get("orderStatus", "")).upper() in _DEAD_LEG_STATUSES
            ]
            all_legs_dead = bool(existing_legs) and len(dead_legs) == len(existing_legs)
            if all_legs_dead:
                dead_status = str(dead_legs[0].get("orderStatus", "?")).upper()
                logger.warning(
                    "reconcile: %s (id=%d) ALL super-order exit leg(s) REJECTED/"
                    "CANCELLED/EXPIRED (%s) — position cannot self-exit via super "
                    "order. Marking EXIT_LEGS_REJECTED so EOD squareoff fires a "
                    "single plain MARKET SELL instead of looping.",
                    pos.symbol, pos.id, dead_status,
                )
                pos.status = "EXIT_LEGS_REJECTED"
                pos.error_message = (
                    f"Super-order exit leg(s) {dead_status} by Dhan "
                    f"(circuit-limit or surveillance). EOD squareoff will attempt a "
                    f"plain MARKET SELL once."
                )
                db.commit()
                # Fall through — hit_kind is still None, the regular OPEN
                # handling below will `continue` for this position, and EOD
                # squareoff will pick it up next time via status="EXIT_LEGS_REJECTED".

        if hit_kind is None:
            # AUDIT FIX (EOD reconcile path): EOD_SQUAREOFF positions used
            # a plain dhan_client.place_order (MARKET SELL), not a super
            # order — so they will never have a TARGET_LEG or STOP_LOSS_LEG
            # in Dhan's super-order book. Their plain sell order won't even
            # appear in get_super_order_list() (super order list only shows
            # super orders, not plain orders). This point is only reached
            # for an EOD_SQUAREOFF position at all now if
            # _reconcile_eod_pending above could NOT resolve it (no
            # dhan_exit_order_id — a pre-session40 row, or the flat SELL
            # never got accepted at all) — the real-fill path (session40)
            # lives there now, using get_order_list()/dhan_exit_order_id.
            # This remains a fallback: check the top-level ENTRY_LEG of the
            # ORIGINAL super order to get the entry fill, then leave the
            # exit at the known entry_price placeholder. If we cannot find
            # the real fill, leave error_message as-is (still marked
            # PENDING_RECONCILE) for the next pass.
            if pos.status in _FLAT_SELL_PENDING_STATUSES:
                entry_status = str(row.get("orderStatus", "")).upper()
                if entry_status in _FILLED_STATUSES:
                    # Original entry traded — exit was a plain MARKET SELL
                    # whose fill we can't directly read from super_orders.
                    # Use entry_price as exit_price placeholder (already set
                    # by eod_squareoff.py / close_position_now()); clear the
                    # pending-reconcile flag.
                    pos.error_message = None
                    db.commit()
                    logger.info(
                        "reconcile: %s (id=%d) %s — entry leg confirmed traded; "
                        "exit price remains entry_price placeholder (no dhan_exit_order_id "
                        "to resolve the real flat-SELL fill via get_order_list())",
                        pos.symbol, pos.id, pos.status,
                    )
                continue

            # Entry itself never filled and is now dead (rejected/cancelled
            # on Dhan's side) — release capital, mark as error, move on.
            entry_status = str(row.get("orderStatus", "")).upper()
            # AUDIT FIX: Dhan's /v2/super/orders response omits the
            # `legName` key on the parent (top-level) row in some SDK
            # versions — the module docstring already flags this payload
            # shape as "confirmed via docs, not live-tested". Guarding
            # against a missing `legName` here: if Dhan doesn't include
            # it, we still treat the parent row as the ENTRY_LEG (it's
            # the only row at the top level by definition) and apply the
            # same REJECTED/CANCELLED logic, which is correct. Without
            # this guard, a Dhan response with no `legName` on the parent
            # would silently skip the dead-entry cleanup path entirely,
            # leaving the position stuck as OPEN and capital locked.
            leg_name = row.get("legName", "ENTRY_LEG")
            if leg_name in ("ENTRY_LEG", "") and entry_status in _DEAD_ENTRY_STATUSES:
                pos.status = "ERROR"
                pos.error_message = f"Entry leg {entry_status} on Dhan (reconciled)"
                pos.closed_at = datetime.now(timezone.utc)
                db.commit()
                ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
                # BUG FIX (Issue #4): symbol lock was never released on the
                # dead-entry path — capital was freed but the SharedSymbolLock
                # row was left behind, permanently blocking re-entry into this
                # symbol (e.g. TREL stuck with held_by_mode=null). Fixed by
                # mirroring the same release() call every other exit path makes.
                shared_symbol_lock.release(db, pos.symbol)
                closed += 1
                logger.warning(
                    "reconcile: %s (id=%d) entry leg %s — capital released, no trade",
                    pos.symbol, pos.id, entry_status,
                )
            continue

        exit_price = _extract_leg_price(
            hit_leg, row,
            own_fallback_price=(pos.target_price if hit_kind == "TARGET_HIT" else pos.stop_price),
        )
        realized_pnl = (exit_price - pos.entry_price) * pos.quantity
        realized_pnl_pct = (
            (exit_price - pos.entry_price) / pos.entry_price * 100.0
            if pos.entry_price else 0.0
        )

        # eod_squareoff.py / close_position_now() already returned this position's capital_risked to the
        # ledger when they PLACED the flat SELL; only the P&L is new. (Mirrors _resolve_pending_with_price.)
        _capital_already_released = pos.status in _FLAT_SELL_PENDING_STATUSES
        pos.status = hit_kind
        pos.exit_price = exit_price
        pos.realized_pnl = realized_pnl
        pos.realized_pnl_pct = realized_pnl_pct
        pos.dhan_exit_order_id = str(hit_leg.get("orderId") or pos.dhan_super_order_id)
        pos.error_message = None  # session72: clear a stale earlier-EOD failure text on a real close
        pos.closed_at = datetime.now(timezone.utc)
        db.commit()

        ledger.release_capital(
            db,
            position_value=0.0 if _capital_already_released else pos.capital_risked,
            realized_pnl=realized_pnl,
        )
        # AUDIT FIX (session60): position is now fully flat — release this
        # service's cross-service symbol lock claim so the symbol becomes
        # buyable again by either service. See capital/shared_symbol_lock.py.
        shared_symbol_lock.release(db, pos.symbol)
        closed += 1
        logger.info(
            "reconcile: %s (id=%d) %s @ ₹%.2f — P&L ₹%.2f (%.2f%%)",
            pos.symbol, pos.id, hit_kind, exit_price, realized_pnl, realized_pnl_pct,
        )
        # BUG FIX (this session — "no notification got for position stocks
        # order on telegram"): notifier.py has existed since session41 but
        # was ONLY ever wired to CRITICAL failure paths — a normal,
        # successful TARGET_HIT/STOP_HIT close (the vast majority of this
        # service's exits) never notified anyone at all, unlike
        # real-trade-service's exit_engine.py which notifies on every SELL
        # it sends. Added here so a closed scalp position is always
        # reported, good or bad.
        emoji = "🟢" if hit_kind == "TARGET_HIT" else "🔴"
        notifier.notify_sync(
            f"{emoji} <b>{hit_kind}</b> — {pos.symbol} x{pos.quantity}\n"
            f"Entry ₹{pos.entry_price:.2f} → Exit ₹{exit_price:.2f}\n"
            f"P&L ₹{realized_pnl:,.2f} ({realized_pnl_pct:.2f}%)"
        )

    return closed + overnight_closed


def run_retention_cleanup(db: Session) -> int:
    """this session — user asked for Trade History to only keep "today" /
    "last 3 days" and for the ledger to actually only STORE that much, not
    just display a filtered view of an ever-growing table.

    Deletes CLOSED scalp positions (any terminal status — TARGET_HIT,
    STOP_HIT, EOD_SQUAREOFF, MANUAL_EXIT, STAGNATION_EXIT, ERROR) whose
    closed_at (or opened_at, for the ERROR case where closed_at is often
    never set — see models.py's ScalpPosition.closed_at usage) is older
    than config.TRADE_HISTORY_RETENTION_DAYS.

    Deliberately NEVER deletes OPEN or EXIT_LEGS_REJECTED rows regardless
    of age — those are live broker exposure, not history; deleting one
    would silently break the capacity gate (orders/entry.py::
    _count_open_positions) and same-symbol re-entry cooldown
    (_get_reentry_block_reason), both of which query ScalpPosition
    directly. This function only ever touches genuinely finished trades.

    Called at most once per IST calendar day from main.py's
    _fast_reconcile_loop (see ScalpGateState.retention_cleanup_last_run_
    date), and available on-demand via POST /trades/cleanup. Returns the
    number of rows deleted."""
    from sqlalchemy import or_, and_

    cutoff = datetime.now(timezone.utc) - timedelta(days=config.TRADE_HISTORY_RETENTION_DAYS)
    terminal_statuses = (
        "TARGET_HIT", "STOP_HIT", "EOD_SQUAREOFF", "MANUAL_EXIT", "STAGNATION_EXIT", "ERROR",
    )
    rows = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(terminal_statuses))
        .filter(
            or_(
                ScalpPosition.closed_at < cutoff,
                and_(ScalpPosition.closed_at.is_(None), ScalpPosition.opened_at < cutoff),
            )
        )
        .all()
    )
    n = len(rows)
    if n:
        for r in rows:
            db.delete(r)
        db.commit()
        logger.info(
            "retention-cleanup: deleted %d closed position(s) older than %.0f day(s)",
            n, config.TRADE_HISTORY_RETENTION_DAYS,
        )
    return n
