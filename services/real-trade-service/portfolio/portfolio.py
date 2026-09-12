"""
portfolio/portfolio.py — DEMO-mode fill simulation + account/position
bookkeeping. This is the "not a fake/simple simulator" piece from the
original plan: it uses the SAME entry rules (bounded limit price,
time-boxed validity — config.ENTRY_ZONE_UPPER_PCT/ENTRY_VALIDITY_MINUTES)
that a REAL order would use, and prices fills off the real market_feed
quote, not a synthetic random walk.

Simplifications this phase is explicit about (a real broker adds more
nuance than this):
  * Fill assumption: a DEMO limit order fills at min(limit_price, ltp) the
    moment ltp is at or below the limit — i.e. no partial fills, no queue
    position modeling, no slippage beyond "you don't get a better price
    than your own limit". This is a conservative simplification (real
    fills are sometimes worse due to slippage on illiquid names) — it will
    NOT make DEMO mode look better than REAL mode would perform, which is
    the direction that matters for trusting the rehearsal.
  * No market-order fallback — matches config.ENTRY_NO_CHASE (decision 1):
    an unfilled DEMO entry expires exactly like a real one would.

REAL mode never calls this module for fills — a REAL fill only ever comes
from Dhan's own order/trade webhook or reconciliation (Phase 3). This
module's `record_real_fill()` exists only so REAL positions can be tracked
in the SAME shape once that's wired, without changing this schema again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import models
from audit.logger import log_action
from market_feed.feed import Tick

logger = logging.getLogger("real-trade-portfolio")


def get_account(db: Session, mode: str) -> models.TradeAccount:
    row = db.query(models.TradeAccount).filter_by(mode=mode).first()
    if row is None:
        raise RuntimeError(f"No trade_accounts row for mode={mode} — schema not seeded.")
    return row


def open_positions(db: Session, mode: str) -> list[models.TradePosition]:
    """Both OPEN and PARTIALLY_CLOSED count as 'live' here — a position
    that's had a partial exit still has shares that need trailing-stop,
    time-stop, and eventual full-exit evaluation. Only CLOSED positions
    are excluded. (A query that only matched status='OPEN' would silently
    stop evaluating a position's remaining shares the moment its first
    partial exit fired — exactly the kind of orphaned-state bug this
    session has been hunting elsewhere in the codebase.)

    PENDING_EXIT is deliberately excluded here — a position whose exit
    was already sent to Dhan and is awaiting fill confirmation shouldn't
    be re-evaluated for a NEW exit decision this cycle (exit_engine's
    _has_pending_real_sell guards the same case; excluding it here just
    avoids the redundant pass). See main.py's /positions/{mode} endpoint,
    which explicitly re-includes PENDING_EXIT for display, for why that
    exclusion is scoped to re-evaluation and not to "does this symbol
    still represent held exposure" — see held_exposure_positions() below
    for that second, distinct question.
    """
    return (
        db.query(models.TradePosition)
        .filter(models.TradePosition.mode == mode, models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")))
        .all()
    )


def held_exposure_positions(db: Session, mode: str) -> list[models.TradePosition]:
    """
    BUG FIX (2026-09-07): risk_engine's no-pyramiding check and portfolio-risk
    cap (see entry_engine/entry.py's _account_state -> open_position_symbols /
    open_positions_total_risk) were built on top of open_positions() above —
    which deliberately EXCLUDES status="PENDING_EXIT" (an exit already sent to
    Dhan but not yet fill-confirmed) so the EXIT cycle doesn't re-evaluate it.
    That exclusion is correct for the exit loop, but entry.py's reuse of the
    same helper for "does this symbol already have exposure" is a different
    question with a different right answer: a PENDING_EXIT position still has
    real shares sitting in the account until Dhan confirms the sell — they
    just aren't visible to open_position_symbols, so the no-pyramiding guard
    (risk_engine/engine.py check #7) silently lets a fresh BUY candidate for
    that same symbol through risk-approval while the previous exit is still
    in flight, and open_positions_total_risk understates real exposure by the
    same amount. Use this (not open_positions()) anywhere the question is
    "does the account currently hold this symbol" rather than "which
    positions need a fresh exit decision this cycle."
    """
    return (
        db.query(models.TradePosition)
        .filter(
            models.TradePosition.mode == mode,
            models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED", "PENDING_EXIT")),
        )
        .all()
    )


def import_broker_holdings(db: Session) -> int:
    """Bring pre-existing Dhan demat holdings — stocks bought manually, or
    already sitting in the account before Stockky started trading it —
    into this system's own TradePosition table, so the Positions/Orders
    tabs and exit_engine can see and manage them too.

    THE GAP THIS CLOSES: Stockky only ever creates a TradePosition row
    when ITS OWN entry_engine places and fills a BUY. A holding that was
    never bought through this app (checked against a user's Portfolio.csv
    export: e.g. Devyani International, Paradeep Phosphates, Suzlon
    Energy, Vodafone Idea) has no TradeOrder/TradePosition row at all —
    invisible to /positions and /orders, and invisible to exit_engine's
    open_positions() query, so nothing ever evaluates a stop/target for it
    and nothing ever sells it automatically. dhan_client.get_holdings()
    already existed (wired to a read-only /dhan/holdings admin endpoint)
    but was never used to populate this system's own tracking tables —
    this function is that missing wiring.

    Idempotent: only creates a row for a symbol with NO existing REAL
    position in OPEN/PARTIALLY_CLOSED/PENDING_EXIT — safe to call every
    cycle (see reconcile_real_orders), never double-imports.

    BUG FIX (2026-09-08): the CLOSED status was never excluded here, only
    the three "still live" statuses above. Sequence that broke: this
    system fully sells a position -> record_real_exit_fill() marks it
    CLOSED -> next cycle, THIS function runs again (unconditionally,
    before exit checks) and re-fetches Dhan's holdings snapshot -> if
    that snapshot still lists the symbol (broker-side settlement/holdings
    -feed lag after a same-day sell — not instant), the already_tracked
    query above found nothing (CLOSED wasn't in its filter) and created a
    brand-new phantom OPEN position for a stock that was already fully
    sold. exit_engine then re-evaluated it, tried to sell shares that no
    longer existed, Dhan rejected with "insufficient holding quantity" /
    "scrip limit insufficient", and that got misrouted into a "CDSL
    authorization required" alert for an already-closed position (see
    live case: WELSPLSOL showed Orders-tab SELL 15/15 Success while
    Telegram simultaneously claimed it was still eDIS-blocked for the
    same qty). Fix: also treat a CLOSED position as "already tracked" —
    i.e. don't re-import — if it closed within the settlement-lag
    window (same UTC calendar day), via _RECENT_CLOSE_REIMPORT_GUARD_HOURS
    below. An older CLOSED position (a prior day's fully-exited trade)
    still falls through and gets re-imported normally, e.g. if the user
    manually re-buys the same symbol later outside this app.

    Deliberately does NOT touch account.cash_available: these shares were
    never bought through this system's own ledger, so there's no matching
    cash deduction to make — this is monitoring/exit management only, not
    a real fill event.

    No proposed_stop/target exists for a position that was never opened
    via a TradeDecision, so this uses the same flat-percentage fallback
    entry_engine/exit_engine already fall back to elsewhere
    (FLAT_STOP_PCT/FLAT_TARGET_PCT) rather than inventing a third
    convention — applied around the BROKER'S OWN avgCostPrice, not a
    fresh live price, so the stop/target reflects the actual cost basis.
    """
    from entry_engine.entry import FLAT_STOP_PCT, FLAT_TARGET_PCT
    from execution import dhan_client
    from datetime import timedelta

    # How long a just-CLOSED position stays protected from re-import even
    # though Dhan's holdings feed may still list it (settlement-lag
    # window). 24h comfortably covers same-day T+1 lag without
    # permanently blocking a genuine later re-buy of the same symbol.
    _RECENT_CLOSE_REIMPORT_GUARD_HOURS = 24

    try:
        holdings = dhan_client.get_holdings(db)
    except Exception as e:  # noqa: BLE001 — never block a reconcile cycle over this
        logger.warning("import_broker_holdings: could not fetch Dhan holdings: %s", e)
        return 0

    def _get(row: dict, *keys, default=None):
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        return default

    imported = 0
    now = datetime.now(timezone.utc)
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        # Dhan v2's confirmed field names (dhanhq.co/docs/v2/portfolio) are
        # tradingSymbol/totalQty/avgCostPrice — snake_case fallbacks kept
        # defensively in case a future SDK version renames them, same
        # idiom already used throughout reconcile.py for this exact class
        # of uncertainty.
        raw_symbol = _get(row, "tradingSymbol", "trading_symbol", "symbol")
        qty = _get(row, "totalQty", "total_qty", "quantity")
        avg_price = _get(row, "avgCostPrice", "avg_cost_price", "average_price")
        if not raw_symbol or not qty or not avg_price:
            continue
        try:
            qty = int(qty)
            avg_price = float(avg_price)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or avg_price <= 0:
            continue
        symbol = str(raw_symbol).upper().strip()

        already_tracked = db.query(models.TradePosition).filter(
            models.TradePosition.mode == "REAL",
            models.TradePosition.symbol == symbol,
            models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED", "PENDING_EXIT")),
        ).first()
        if already_tracked is not None:
            continue

        # See BUG FIX (2026-09-08) in the docstring above: guard against
        # re-importing a symbol we JUST closed ourselves, before Dhan's
        # holdings feed has caught up to the sell.
        recent_close_guard = now - timedelta(hours=_RECENT_CLOSE_REIMPORT_GUARD_HOURS)
        recently_closed = db.query(models.TradePosition).filter(
            models.TradePosition.mode == "REAL",
            models.TradePosition.symbol == symbol,
            models.TradePosition.status == "CLOSED",
            models.TradePosition.closed_at.isnot(None),
            models.TradePosition.closed_at >= recent_close_guard,
        ).first()
        if recently_closed is not None:
            logger.info(
                "import_broker_holdings: skipping re-import of %s — closed by this "
                "system at %s, within the %dh settlement-lag guard window.",
                symbol, recently_closed.closed_at, _RECENT_CLOSE_REIMPORT_GUARD_HOURS,
            )
            continue

        stop_price = round(avg_price * (1 - FLAT_STOP_PCT / 100.0), 2)
        target_price = round(avg_price * (1 + FLAT_TARGET_PCT / 100.0), 2)
        position = models.TradePosition(
            mode="REAL", symbol=symbol, status="OPEN",
            qty_open=qty, avg_entry_price=avg_price, opened_at=now,
            current_stop=stop_price, current_target=target_price,
            initial_stop_distance=abs(avg_price - stop_price),
            # 2026-09-09 fix: opened_at above is the IMPORT moment, not the
            # real purchase date (Dhan's holdings API doesn't give us that) —
            # exit_engine._send_real_sell used to read opened_at=="today" as
            # "this was bought and is being sold same-day" and sell it
            # product_type="INTRADAY". For a real demat holding with no
            # matching MIS position, Dhan treats that as opening a fresh
            # short and margin-rejects it ("insufficient funds") instead of
            # squaring off. broker_imported=True tells exit.py to always use
            # CNC for this position, independent of opened_at.
            broker_imported=True,
        )
        db.add(position)
        db.flush()
        db.add(models.TradePositionEvent(
            position_id=position.id, event_type="OPENED",
            detail=f"Imported from Dhan demat holdings (pre-existing, not bought via this "
                   f"app): {qty} @ avg cost ₹{avg_price}, flat {FLAT_STOP_PCT}%/{FLAT_TARGET_PCT}% "
                   f"stop/target since no decision/proposed_stop exists for it",
        ))
        logger.info(
            "import_broker_holdings: imported %s (%d shares @ avg ₹%.2f) as a new tracked "
            "REAL position — now visible to /positions and exit_engine", symbol, qty, avg_price,
        )
        imported += 1

    if imported:
        db.commit()
    return imported


def holdings_sync_reconcile(db: Session) -> dict:
    """The mirror-image fix to import_broker_holdings() above, and the
    other half of the "19 OPEN positions in Stockky's DB vs 4 real Dhan
    holdings" incident (2026-09-08, SyncContext STOCKKY decision #29).

    import_broker_holdings() brings a broker holding INTO this system when
    Dhan has a stock this system doesn't know about. This function does
    the opposite: it force-CLOSES a REAL position this system believes is
    OPEN/PARTIALLY_CLOSED when Dhan's own holdings + live positions
    snapshot shows the account does NOT actually hold that symbol any
    more (or never settled into one).

    ROOT CAUSE: reconcile_real_orders() books a position OPEN the moment
    Dhan's orderbook (get_order_list) reports an order status of
    TRADED/COMPLETE/PART_TRADED — that only proves the order was accepted
    and reported executed at the exchange, never that the resulting
    shares are still (or ever were) actually sitting in the demat/CNC
    account by the time a later cycle runs. A same-day CNC buy that Dhan
    later reverses (AMO/BO leg cancellation, margin shortfall caught at
    settlement, an intraday product auto-square-off, etc.) leaves this
    service's own books permanently "OPEN" for shares that no longer (or
    never did) exist at the broker — invisible to Dhan's own Portfolio
    page, but still shown here, still being risk-checked by
    risk_engine's no-pyramiding logic, and still being evaluated for
    stop/target exits that will only ever fail with a broker rejection
    (see the PARADEEP "Invalid SecurityId" / "Validate Qty from CDSL"
    incident this same session — some of those rejections were this exact
    ghost-position class of bug, not a security-id or CDSL-authorization
    problem at all).

    THE FIX: build the set of symbols Dhan actually reports (demat
    holdings + live intraday/CNC positions, unioned — a fresh same-day CNC
    buy shows up in get_positions() before it settles into
    get_holdings(), so BOTH must be checked or a same-day genuine holding
    would be wrongly treated as a ghost). Any REAL OPEN/PARTIALLY_CLOSED
    position whose symbol is NOT in that set, AND that's already older
    than config.HOLDINGS_SYNC_GUARD_MINUTES (protects a position that
    filled moments ago from being force-closed before Dhan's own
    positions/holdings feed has had a chance to catch up — same
    settlement-lag idea as import_broker_holdings' own
    _RECENT_CLOSE_REIMPORT_GUARD_HOURS, just on the opposite side of the
    same race), is force-closed here: qty_open -> 0, status -> CLOSED,
    realized_pnl left untouched (0 contribution — there's no broker-
    confirmed exit fill/price to book a real P&L against, unlike a normal
    sell), and the cash this system speculatively deducted at "fill" time
    (record_real_fill) is refunded so cash_available doesn't stay
    permanently short for shares that were never actually held.

    PENDING_EXIT is deliberately excluded (unlike OPEN/PARTIALLY_CLOSED)
    — a position already mid-exit has its own dedicated orphan-repair path
    (_repair_orphaned_pending_exits in execution/reconcile.py) that reverts
    it back to a live status once its SELL order is confirmed dead; running
    this ghost-check on it too would race that logic.

    2026-09-09 addition (same session): the full-ghost-close check above
    only ever fired when the broker reports ZERO of a symbol. A symbol
    that's still held but in a SMALLER quantity than qty_open (partial
    sell placed outside this app, a BO/CO leg partially reversed, etc.)
    used to pass straight through untouched — this was the "ANDHRAPAP
    still shows qty 4 (Dhan holds 2) even after clicking Reconcile"
    report. Broker qty is now tracked per-symbol (not just presence), so
    a position whose broker qty is positive but LESS than qty_open gets
    qty_open capped down to match (status flipped OPEN -> PARTIALLY_CLOSED
    where relevant) with the same avg-entry-price cash refund logic as a
    full ghost close, just scaled to the missing shares only. A broker
    qty >= qty_open is left alone (nothing stale to fix).

    Idempotent and safe to call every cycle: a position already CLOSED by
    this pass (or any other path) is excluded by the status filter, and a
    qty_open already capped to match the broker has nothing left to sync
    on the next pass. Never raises — mirrors import_broker_holdings' own
    "must never block the rest of the cycle" contract.
    """
    from datetime import timedelta
    from execution import dhan_client

    try:
        holdings = dhan_client.get_holdings(db)
    except Exception as e:  # noqa: BLE001 — never block a reconcile cycle over this
        logger.warning("holdings_sync_reconcile: could not fetch Dhan holdings: %s", e)
        return {"closed": 0, "symbols": []}
    try:
        live_positions = dhan_client.get_positions(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("holdings_sync_reconcile: could not fetch Dhan positions: %s", e)
        return {"closed": 0, "symbols": []}

    def _get(row: dict, *keys, default=None):
        for k in keys:
            if k in row and row[k] not in (None, ""):
                return row[k]
        return default

    def _qty(row: dict, *keys) -> int:
        raw = _get(row, *keys, default=0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # 2026-09-09 fix (this session): this used to be a bare `set()` of
    # symbols Dhan reports, which only ever let this function answer
    # "does the broker still hold ANY of this symbol?" — a position that
    # was PARTIALLY sold outside this app (manually via the broker's own
    # app, a same-day BO/CO leg, a CDSL auto-square-off, etc.) still has
    # its symbol in that set, so it fell straight through the `continue`
    # below and was never corrected. That's the exact "ANDHRAPAP still
    # shows qty 4 after Reconcile" bug: Dhan holds 2, Stockky's DB still
    # has qty_open=4, and because 4 > 0 the symbol was never a "ghost" so
    # this function silently did nothing to it, cycle after cycle,
    # Reconcile click after Reconcile click. Tracking the actual qty (not
    # just presence) lets the loop below cap qty_open DOWN to what the
    # broker really holds, the same correction exit_engine/exit.py's
    # oversell branch already makes reactively (only ever triggered by a
    # failed SELL attempt) — this makes it proactive, on every reconcile,
    # so a stale qty is fixed before it ever causes an oversell rejection.
    broker_qty_by_symbol: dict[str, int] = {}
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        sym = _get(row, "tradingSymbol", "trading_symbol", "symbol")
        qty = _qty(row, "totalQty", "total_qty", "quantity")
        if sym and qty > 0:
            key = str(sym).upper().strip()
            broker_qty_by_symbol[key] = max(broker_qty_by_symbol.get(key, 0), qty)
    for row in live_positions or []:
        if not isinstance(row, dict):
            continue
        sym = _get(row, "tradingSymbol", "trading_symbol", "symbol")
        # Dhan's positions payload can carry both a long buy qty and a
        # short sell qty on the same row (netQty is the settled figure,
        # positiveQty/buyQty is the alternate spelling seen on some SDK
        # versions) — any of these being > 0 means the account currently
        # has exposure in this symbol, which is all this check needs.
        qty = max(
            _qty(row, "netQty", "net_qty"),
            _qty(row, "positiveQty", "positive_qty", "buyQty", "buy_qty"),
        )
        if sym and qty > 0:
            key = str(sym).upper().strip()
            broker_qty_by_symbol[key] = max(broker_qty_by_symbol.get(key, 0), qty)
    broker_symbols: set[str] = set(broker_qty_by_symbol.keys())

    now = datetime.now(timezone.utc)
    guard_cutoff = now - timedelta(minutes=config.HOLDINGS_SYNC_GUARD_MINUTES)

    candidates = (
        db.query(models.TradePosition)
        .filter(
            models.TradePosition.mode == "REAL",
            models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
        )
        .all()
    )

    closed_symbols: list[str] = []
    synced_symbols: list[str] = []
    account = None
    # BUG FIX (2026-09-10, session21c audit): local import (not top-level —
    # exit_engine.exit imports from this module at module scope, so a
    # top-level import here would be circular).
    from exit_engine.exit import _has_pending_real_sell
    for position in candidates:
        symbol = (position.symbol or "").upper().strip()

        # BUG FIX (2026-09-10, session21c audit): the PENDING_EXIT exclusion
        # on the candidates query above (see this function's own docstring)
        # only protects a FULL exit in flight — record_real_exit_sent's
        # full=False path (a partial target-hit exit) deliberately leaves
        # the position at OPEN/PARTIALLY_CLOSED, so it was never excluded
        # here, and this function has no other way to tell "shares are
        # missing because our own SELL just filled at the broker" apart
        # from "shares are missing for some other reason" (manual sell
        # outside this app, a reversed leg, etc.).
        #
        # Reachable window: exit_engine sends a partial SELL (MARKET order —
        # NSE typically fills these within the same second) earlier in this
        # same cycle (cycle_runner runs the exit stage before the reconcile
        # stage). By the time THIS function's own Dhan holdings/positions
        # calls run, moments later in the SAME reconcile_real_orders() call,
        # the broker may already reflect the reduced quantity — while the
        # TradeOrder row is still "PLACED" because the pending_orders loop
        # further down in reconcile_real_orders() (which is what actually
        # books the fill via record_real_exit_fill) hasn't reached it yet.
        #
        # Without this check, this function would mis-read that gap as an
        # "external partial sell" and cap qty_open down + refund cash at
        # avg_entry_price (cost basis) right here — and then the pending-
        # orders loop, moments later, would ALSO book the same fill via
        # record_real_exit_fill (qty_open -= qty_closed again, cash +=
        # exit_price*qty_closed again): the position's qty_open gets
        # decremented twice for one real-world sell, and cash_available is
        # credited twice (once at the wrong cost-basis figure, once at the
        # correct exit price) — silently corrupting both qty and cash for
        # every partial REAL exit this ever raced, not just an edge case.
        # Skipping here is always safe: the in-flight SELL is already being
        # handled by the pending-orders loop this same cycle (or the next
        # one), which is the ONLY path allowed to book a confirmed fill.
        if _has_pending_real_sell(db, symbol):
            continue

        opened_at = position.opened_at
        if opened_at is not None and opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        too_recent = opened_at is not None and opened_at > guard_cutoff

        if symbol in broker_symbols:
            # Broker still holds SOME of this symbol — not a ghost — but
            # it may hold LESS than qty_open (partial external sell).
            # Cap qty_open down to match; never raise it (a broker qty
            # >= ours just means our own not-yet-reconciled buy hasn't
            # settled into the feed yet, nothing to fix here).
            if too_recent:
                continue  # too recent — broker feed may just not have caught up yet
            broker_have = broker_qty_by_symbol.get(symbol, 0)
            qty_open = position.qty_open or 0
            if broker_have >= qty_open or qty_open <= 0:
                continue
            diff = qty_open - broker_have
            refund = round((position.avg_entry_price or 0.0) * diff, 2)
            position.qty_open = broker_have
            if position.status == "OPEN" and broker_have < qty_open:
                position.status = "PARTIALLY_CLOSED"
            db.add(models.TradePositionEvent(
                position_id=position.id, event_type="QTY_SYNCED",
                detail=(
                    f"holdings_sync_reconcile: {symbol} qty_open was {qty_open}, broker "
                    f"holds {broker_have} — capped down by {diff} share(s) (sold outside "
                    f"this app or never fully settled), cash refunded ₹{refund:,.2f} at "
                    f"avg entry ₹{position.avg_entry_price}"
                ),
            ))
            logger.warning(
                "holdings_sync_reconcile: synced qty_open for %s (id=%d): %d -> %d "
                "(broker holds %d), refunding ₹%.2f",
                symbol, position.id, qty_open, broker_have, broker_have, refund,
            )
            if account is None:
                account = get_account(db, "REAL")
            account.cash_available += refund
            synced_symbols.append(symbol)
            continue

        if too_recent:
            continue  # too recent — broker feed may just not have caught up yet

        qty_open = position.qty_open or 0
        refund = round((position.avg_entry_price or 0.0) * qty_open, 2)

        position.qty_open = 0
        position.status = "CLOSED"
        position.closed_at = now
        db.add(models.TradePositionEvent(
            position_id=position.id, event_type="GHOST_CLOSED",
            detail=(
                f"holdings_sync_reconcile: {symbol} not found in Dhan holdings or live "
                f"positions (checked {len(broker_symbols)} broker symbols) — force-closed "
                f"{qty_open} shares as a ghost/never-actually-held position, cash refunded "
                f"₹{refund:,.2f} at avg entry ₹{position.avg_entry_price}"
            ),
        ))
        logger.warning(
            "holdings_sync_reconcile: force-closed ghost position %s (id=%d) — "
            "%d shares not found at broker, refunding ₹%.2f",
            symbol, position.id, qty_open, refund,
        )
        if account is None:
            account = get_account(db, "REAL")
        account.cash_available += refund
        closed_symbols.append(symbol)

    if closed_symbols or synced_symbols:
        if account is not None:
            account.current_equity = account.cash_available + _open_positions_market_value(db, "REAL")
            account.updated_at = now
        db.commit()
        if closed_symbols:
            log_action(
                db, actor="system", action="HOLDINGS_SYNC_CLOSED", mode="REAL",
                detail=f"force-closed {len(closed_symbols)} ghost position(s) not found at broker: "
                       f"{', '.join(closed_symbols)}",
            )
        if synced_symbols:
            log_action(
                db, actor="system", action="HOLDINGS_SYNC_QTY_CAPPED", mode="REAL",
                detail=f"capped qty_open for {len(synced_symbols)} position(s) with partial "
                       f"external sells: {', '.join(synced_symbols)}",
            )

    return {"closed": len(closed_symbols), "symbols": closed_symbols, "synced": synced_symbols}


def try_fill_entry(db: Session, order: models.TradeOrder, tick: Tick, stop_price: float, target_price: float) -> bool:
    """DEMO-only. Returns True and records a fill + opens/adds-to a
    position if the current tick would fill this pending limit BUY order;
    False (order left PENDING) otherwise. Expiry (valid_until) is checked
    by the caller (entry_engine), not here — this function only ever
    answers "would this order fill right now".

    stop_price/target_price come from the TradeDecision that produced this
    order (TradeOrder itself has no stop/target columns — only the
    decision does) and are written onto the position at OPEN time so
    exit_engine has something to trail/check from the very first cycle,
    not just after its own first evaluation."""
    if order.mode != "DEMO":
        raise RuntimeError("try_fill_entry is DEMO-only — REAL fills come from Dhan, never simulated.")
    if order.side != "BUY" or order.status != "PLACED":
        return False
    if order.limit_price is None or tick.price > order.limit_price:
        return False  # price hasn't come down into the entry zone yet

    fill_price = min(order.limit_price, tick.price)
    now = datetime.now(timezone.utc)

    order.status = "FILLED"
    order.updated_at = now
    db.add(models.TradeOrderEvent(order_id=order.id, event_type="FILLED",
                                   detail=f"Simulated DEMO fill @ {fill_price}"))
    db.add(models.TradeFill(order_id=order.id, qty=order.qty, price=fill_price, filled_at=now))

    # BUG FIX (2026-09-10, session21 audit): this used to filter status="OPEN"
    # only. open_positions()/held_exposure_positions() both treat PARTIALLY_
    # CLOSED as still-live (a position with an existing partial exit still
    # holds real shares), and risk_engine's no-pyramiding check is built on
    # that same "OPEN or PARTIALLY_CLOSED" definition — so with
    # allow_pyramiding=True, a fresh BUY fill for a symbol whose existing
    # position had already taken a partial target-hit exit (status
    # PARTIALLY_CLOSED, not OPEN) found nothing here and opened a SECOND,
    # entirely separate TradePosition row for the same symbol instead of
    # averaging into the existing one. Two live rows for one symbol then
    # race each other through exit_engine (each independently evaluated for
    # stop/target) and confuse any single-row lookup (e.g.
    # record_real_exit_fill/close_position's own by-symbol queries use
    # .first(), so a SELL could silently apply to the wrong one). Matching
    # the status set risk_engine already treats as "this symbol has
    # exposure" closes the gap.
    position = db.query(models.TradePosition).filter(
        models.TradePosition.mode == "DEMO",
        models.TradePosition.symbol == order.symbol,
        models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
    ).first()
    if position is None:
        position = models.TradePosition(
            mode="DEMO", symbol=order.symbol, status="OPEN",
            qty_open=order.qty, avg_entry_price=fill_price, opened_at=now,
            current_stop=stop_price, current_target=target_price,
            # 2026-09-01 fix: fixed at open so exit_engine's gap-down check
            # has a stable reference distance, not one that drifts as
            # current_stop trails.
            initial_stop_distance=abs(fill_price - stop_price),
            # 2026-09-02 Short-Term Trading Upgrade: thread watchlist origin
            # through so exit_engine._load_profile can apply the right
            # catalyst-aware exit profile. NULL for non-watchlist orders.
            watchlist_entry_id=getattr(order, "watchlist_entry_id", None),
            # 2026-09-12 fix: thread source_tab through too, so
            # exit_engine._load_profile can recognize a volume_shock-origin
            # position even with no watchlist_entry_id. NULL for manual orders.
            source_tab=getattr(order, "source_tab", None),
        )
        db.add(position)
        db.flush()
        db.add(models.TradePositionEvent(position_id=position.id, event_type="OPENED",
                                          detail=f"{order.qty} @ {fill_price}, stop {stop_price}, target {target_price}"))
    else:
        # Pyramiding case — only reachable if risk_engine's check #6 was
        # configured to allow it; recompute a volume-weighted average.
        # Stop/target are intentionally left at the ORIGINAL position's
        # values here rather than overwritten by the new add's numbers —
        # exit_engine's trailing logic owns tightening the stop from here,
        # not a fresh entry signal on an already-open name.
        total_cost = position.avg_entry_price * position.qty_open + fill_price * order.qty
        position.qty_open += order.qty
        position.avg_entry_price = round(total_cost / position.qty_open, 4)
        db.add(models.TradePositionEvent(position_id=position.id, event_type="ADDED",
                                          detail=f"+{order.qty} @ {fill_price}"))

    account = get_account(db, "DEMO")
    account.cash_available -= fill_price * order.qty
    account.updated_at = now

    db.commit()
    log_action(db, actor="system", action="ORDER_FILLED", mode="DEMO",
               detail=f"{order.symbol} BUY {order.qty} @ {fill_price}")
    return True


def close_position(
    db: Session, position: models.TradePosition, tick: Tick, qty_to_close: int, reason: str,
) -> float:
    """DEMO-only full or partial exit at the current tick price. Returns
    the realized P&L booked by this close. Updates the account's
    cash/equity/realized_pnl_today in the same transaction so a reader can
    never observe a half-updated state."""
    if position.mode != "DEMO":
        raise RuntimeError("close_position is DEMO-only in this phase.")
    qty_to_close = min(qty_to_close, position.qty_open)
    if qty_to_close <= 0:
        return 0.0

    now = datetime.now(timezone.utc)
    exit_price = tick.price
    pnl = round((exit_price - position.avg_entry_price) * qty_to_close, 2)

    position.qty_open -= qty_to_close
    position.realized_pnl += pnl
    if position.qty_open <= 0:
        position.status = "CLOSED"
        position.closed_at = now
    else:
        position.status = "PARTIALLY_CLOSED"

    db.add(models.TradePositionEvent(
        position_id=position.id,
        event_type="CLOSED" if position.status == "CLOSED" else "PARTIAL_EXIT",
        detail=f"{reason}: {qty_to_close} @ {exit_price} (pnl {pnl:+.2f})",
    ))
    # Flush before re-querying open positions below — the session is
    # autoflush=False (see db.py), so without this the equity recompute
    # could still see this position's PRE-update status and double-count
    # (or drop) it depending on flush timing. Cheap: one extra round trip,
    # correctness-critical: equity must reflect the close that just
    # happened, not a stale read of it.
    db.flush()

    account = get_account(db, "DEMO")
    proceeds = exit_price * qty_to_close
    account.cash_available += proceeds
    account.realized_pnl_today += pnl
    account.realized_pnl_total += pnl
    account.current_equity = account.cash_available + _open_positions_market_value(db, "DEMO")
    account.updated_at = now

    db.commit()
    log_action(db, actor="system", action="POSITION_CLOSED" if position.status == "CLOSED" else "POSITION_PARTIAL_EXIT",
               mode="DEMO", detail=f"{position.symbol} {reason} qty={qty_to_close} pnl={pnl:+.2f}")
    return pnl


def record_real_order_sent(db: Session, order: models.TradeOrder, dhan_order_id: str) -> None:
    """REAL-only. Marks a TradeOrder as sent to the broker. Does NOT open a
    position or touch account cash — a REAL fill is never assumed just
    because the order was accepted; only reconcile_real_orders() (which
    reads Dhan's own order/trade state) is allowed to do that."""
    order.status = "PLACED"
    order.dhan_order_id = dhan_order_id
    order.updated_at = datetime.now(timezone.utc)
    db.add(models.TradeOrderEvent(order_id=order.id, event_type="PLACED",
                                   detail=f"Sent to Dhan, broker order_id={dhan_order_id}"))
    db.commit()


def record_real_fill(db: Session, order: models.TradeOrder, fill_price: float, filled_qty: int,
                      stop_price: float, target_price: float, is_partial: bool = False) -> None:
    """REAL-only equivalent of try_fill_entry's position-opening half, but
    driven by a CONFIRMED fill from Dhan (reconcile_real_orders), never by
    a simulated price check. Mirrors try_fill_entry's pyramiding/average
    logic so both modes produce the same TradePosition shape.

    `filled_qty` here is always the NEW/incremental qty to book this call
    (reconcile_real_orders is responsible for diffing against
    order.filled_qty_so_far before calling this — see that module's
    docstring) — never the order's cumulative broker-reported qty, or a
    PART_TRADED order would get the same shares added to the position
    twice.

    `is_partial=True` (order still PART_TRADED at the broker — more fills
    may still come) leaves order.status as "PARTIAL" instead of "FILLED",
    so reconcile's next-cycle query (status in PLACED/PARTIAL) keeps
    checking this order for the rest of the fill. Caller still owns
    order.filled_qty_so_far — this function only ever books the position
    side of a fill, same division of responsibility record_real_exit_fill
    already uses for exits."""
    now = datetime.now(timezone.utc)
    order.status = "PARTIAL" if is_partial else "FILLED"
    order.updated_at = now
    event_type = "PARTIAL_FILL" if is_partial else "FILLED"
    detail_verb = "Broker-confirmed partial fill" if is_partial else "Broker-confirmed fill"
    db.add(models.TradeOrderEvent(order_id=order.id, event_type=event_type,
                                   detail=f"{detail_verb} @ {fill_price} x{filled_qty}"))
    db.add(models.TradeFill(order_id=order.id, qty=filled_qty, price=fill_price, filled_at=now))

    # BUG FIX (2026-09-10, session21 audit): same class of bug as
    # try_fill_entry's DEMO path above — status="OPEN" only missed an
    # existing PARTIALLY_CLOSED position (one that already had a partial
    # target-hit exit), so a pyramiding-enabled REAL BUY fill for that
    # symbol opened a duplicate TradePosition row instead of averaging into
    # the existing one. See that fix's comment for the full incident
    # reasoning; the fix is identical here — match the same "still live"
    # status set risk_engine's no-pyramiding check and open_positions()/
    # held_exposure_positions() already use.
    position = db.query(models.TradePosition).filter(
        models.TradePosition.mode == "REAL",
        models.TradePosition.symbol == order.symbol,
        models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
    ).first()
    if position is None:
        position = models.TradePosition(
            mode="REAL", symbol=order.symbol, status="OPEN",
            qty_open=filled_qty, avg_entry_price=fill_price, opened_at=now,
            current_stop=stop_price, current_target=target_price,
            # 2026-09-01 fix: same fixed-at-open distance as the DEMO path.
            initial_stop_distance=abs(fill_price - stop_price),
            # 2026-09-02 Short-Term Trading Upgrade: thread watchlist origin
            # through so exit_engine._load_profile applies the catalyst-aware
            # exit profile. NULL for non-watchlist orders.
            watchlist_entry_id=getattr(order, "watchlist_entry_id", None),
            # 2026-09-12 fix: thread source_tab through too, so
            # exit_engine._load_profile can recognize a volume_shock-origin
            # position even with no watchlist_entry_id. NULL for manual orders.
            source_tab=getattr(order, "source_tab", None),
        )
        db.add(position)
        db.flush()
        db.add(models.TradePositionEvent(position_id=position.id, event_type="OPENED",
                                          detail=f"{filled_qty} @ {fill_price}, stop {stop_price}, target {target_price} (broker-confirmed)"))
    else:
        total_cost = position.avg_entry_price * position.qty_open + fill_price * filled_qty
        position.qty_open += filled_qty
        position.avg_entry_price = round(total_cost / position.qty_open, 4)
        db.add(models.TradePositionEvent(position_id=position.id, event_type="ADDED",
                                          detail=f"+{filled_qty} @ {fill_price} (broker-confirmed)"))

    account = get_account(db, "REAL")
    account.cash_available -= fill_price * filled_qty
    account.updated_at = now
    db.commit()
    log_action(db, actor="system", action="ORDER_FILLED", mode="REAL",
               detail=f"{order.symbol} BUY {filled_qty} @ {fill_price} (broker-confirmed)")


def record_real_exit_sent(db: Session, position: models.TradePosition, dhan_order_id: str,
                           qty: int, reason: str, full: bool = True) -> None:
    """REAL-only. A SELL was sent to Dhan for this position but is not yet
    confirmed filled. `full=True` (stop hit / time stop — the whole open
    qty) marks the position PENDING_EXIT so exit_engine stops evaluating
    it until reconcile_real_orders() confirms the fill. `full=False`
    (a partial target exit) leaves the position's status untouched — the
    remainder is still a live, OPEN position that still needs stop
    trailing and further exit evaluation every cycle; only the specific
    in-flight SELL is tracked, via the TradeOrder row itself, so exit_engine's
    own duplicate-send guard (_has_pending_real_sell) is what prevents a
    second SELL before this one confirms — not the position status."""
    if full:
        position.status = "PENDING_EXIT"
    db.add(models.TradePositionEvent(
        position_id=position.id, event_type="EXIT_SENT",
        detail=f"{reason}: SELL {qty} sent to Dhan, broker order_id={dhan_order_id}",
    ))
    db.commit()


def record_real_exit_fill(db: Session, position: models.TradePosition, exit_price: float,
                           qty_closed: int, reason: str) -> float:
    """REAL-only. Confirmed by reconcile_real_orders() against Dhan's own
    trade book — never called speculatively. Books realized P&L exactly
    like close_position() does for DEMO, so both modes report P&L the
    same way.

    BUG FIX (2026-08-27): a still-open remainder used to be left at status
    "OPEN" (not "PARTIALLY_CLOSED", unlike close_position()'s DEMO
    equivalent). exit_engine's target-hit check guards against firing a
    second partial exit at the same target with `position.status ==
    "OPEN"` — so a REAL position that stayed at "OPEN" after its first
    partial exit could get partial-exited AGAIN next cycle if price was
    still above target, instead of moving on to trailing-stop management
    of the remainder like the module docstring describes. Now matches
    DEMO: status becomes "PARTIALLY_CLOSED", and (for a target-hit partial
    specifically) the stop is moved to breakeven on the remainder, exactly
    like close_position()'s DEMO caller does inline."""
    now = datetime.now(timezone.utc)
    qty_closed = min(qty_closed, position.qty_open)
    pnl = round((exit_price - position.avg_entry_price) * qty_closed, 2)

    position.qty_open -= qty_closed
    position.realized_pnl += pnl
    if position.qty_open <= 0:
        position.status = "CLOSED"
        position.closed_at = now
    else:
        position.status = "PARTIALLY_CLOSED"
        if reason == "target_hit_partial":
            # De-risk the remainder the same way DEMO does — move the stop
            # up to breakeven rather than leaving it at the original,
            # wider stop distance now that some profit is locked in.
            position.current_stop = max(position.current_stop or 0, position.avg_entry_price)

    db.add(models.TradePositionEvent(
        position_id=position.id,
        event_type="CLOSED" if position.status == "CLOSED" else "PARTIAL_EXIT",
        detail=f"{reason}: {qty_closed} @ {exit_price} (pnl {pnl:+.2f}, broker-confirmed)",
    ))
    db.flush()

    account = get_account(db, "REAL")
    account.cash_available += exit_price * qty_closed
    account.realized_pnl_today += pnl
    account.realized_pnl_total += pnl
    account.current_equity = account.cash_available + _open_positions_market_value(db, "REAL")
    account.updated_at = now
    db.commit()
    log_action(db, actor="system",
               action="POSITION_CLOSED" if position.status == "CLOSED" else "POSITION_PARTIAL_EXIT",
               mode="REAL", detail=f"{position.symbol} {reason} qty={qty_closed} pnl={pnl:+.2f} (broker-confirmed)")
    return pnl


def force_close_real_position(db: Session, position: models.TradePosition, note: str) -> float:
    """REAL-only. Force-closes a REAL position with NO broker-confirmed exit
    fill/price to book against — used when the broker itself reports the
    position no longer exists (zero holdings) rather than via a normal SELL
    fill. Mirrors holdings_sync_reconcile()'s own full-ghost-close branch
    (see that function's docstring for the original "19 OPEN vs 4 real
    holdings" incident this idiom comes from): qty_open -> 0, status ->
    CLOSED, realized_pnl left untouched (0 contribution — there's no
    broker-confirmed exit price to book a real P&L against), and the cash
    this system speculatively deducted at "fill" time is refunded so
    cash_available doesn't stay permanently short for shares that are no
    longer actually held.

    BUG FIX (2026-09-10, session21 audit): exit_engine's oversell-error
    handler in _send_real_sell (is_oversell_error branch — "broker holds 0,
    ghost-closing") previously called portfolio.close_position() for this.
    close_position() is explicitly DEMO-only (`if position.mode != "DEMO":
    raise RuntimeError(...)`, first line), and _send_real_sell is only ever
    invoked on REAL positions (evaluate_mode's REAL branch, manual_engine.py's
    REAL confirm-SELL) — so that call raised a RuntimeError every single
    time, which was then silently swallowed by that branch's own
    `except Exception as sync_e` handler and logged as a generic "holdings
    sync failed". Net effect: a REAL position the broker had already fully
    exited was NEVER actually ghost-closed by this path — qty_open stayed
    stale indefinitely, the same oversell SELL kept getting rejected every
    cycle, and the alert the admin received ("Qty sync failed... close_position
    is DEMO-only in this phase.") gave no hint the real fix was already
    written and just needed a REAL-mode version. This function is that
    REAL-mode version, and exit.py's oversell branch now calls it directly.
    Returns 0.0 always (a ghost-close never has a realized P&L to report)."""
    if position.mode != "REAL":
        raise RuntimeError("force_close_real_position is REAL-only — use close_position() for DEMO.")
    now = datetime.now(timezone.utc)
    qty_open = position.qty_open or 0
    refund = round((position.avg_entry_price or 0.0) * qty_open, 2)

    position.qty_open = 0
    position.status = "CLOSED"
    position.closed_at = now
    db.add(models.TradePositionEvent(
        position_id=position.id, event_type="GHOST_CLOSED",
        detail=(
            f"{note}: force-closed {qty_open} shares (broker reports 0 held), "
            f"cash refunded ₹{refund:,.2f} at avg entry ₹{position.avg_entry_price}"
        ),
    ))
    db.flush()

    account = get_account(db, "REAL")
    account.cash_available += refund
    account.current_equity = account.cash_available + _open_positions_market_value(db, "REAL")
    account.updated_at = now
    db.commit()
    log_action(db, actor="system", action="POSITION_CLOSED", mode="REAL",
               detail=f"{position.symbol} {note} qty={qty_open} (ghost-close, no broker fill/P&L)")
    return 0.0


def _open_positions_market_value(db: Session, mode: str) -> float:
    """Best-effort mark-to-market using each position's last known price
    (avg_entry_price as a floor when no fresher tick has been recorded
    this cycle — refresh_unrealized() below is what keeps this current).

    BUG FIX (2026-09-07): used open_positions() (excludes PENDING_EXIT), but
    this feeds current_equity for REAL too (see execution/equity_sync.py) — a
    position whose exit was sent to Dhan but not yet fill-confirmed still
    physically holds those shares, so its value belongs in equity until the
    sell actually clears. Excluding it understated REAL equity (and, via
    _account_state, the risk-per-trade sizing derived from it) for as long as
    an exit stayed in flight. Use held_exposure_positions() — same status set
    main.py's /positions endpoint already treats as "still held."
    """
    total = 0.0
    for p in held_exposure_positions(db, mode):
        total += p.avg_entry_price * p.qty_open
    return total


def refresh_unrealized(db: Session, mode: str, ticks: dict[str, Tick]) -> None:
    """Called once per evaluation cycle with the latest ticks for every
    open-position symbol — updates each position's unrealized_pnl and the
    account's current_equity to reflect live prices, not just entry
    prices. Cheap and idempotent; safe to call even with a partial tick
    dict (positions with no fresh tick keep their last-known valuation)."""
    positions = open_positions(db, mode)
    market_value = 0.0
    for p in positions:
        tick = ticks.get(p.symbol)
        last_price = tick.price if tick else p.avg_entry_price
        p.unrealized_pnl = round((last_price - p.avg_entry_price) * p.qty_open, 2)
        market_value += last_price * p.qty_open

    account = get_account(db, mode)
    account.current_equity = round(account.cash_available + market_value, 2)
    account.updated_at = datetime.now(timezone.utc)
    db.commit()
