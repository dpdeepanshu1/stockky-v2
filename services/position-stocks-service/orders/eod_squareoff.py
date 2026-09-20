"""
orders/eod_squareoff.py — 3:00 PM IST hard flat sweep.

Force-closes ALL open scalp positions at market price, no exceptions.
Called once per trading day by the main loop at EOD_SQUAREOFF_TIME_IST.
Uses a once-per-day guard (ScalpGateState.eod_squareoff_fired_date) so
accidental repeated calls do nothing.

Mirrors real-trade-service's own EOD squareoff convention (15:00 IST,
same shape) but is completely independent — if real-trade-service's EOD
sweep hangs, this one still runs, and vice versa.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

import config
import notifier
from capital import ledger, shared_order_budget, shared_symbol_lock
from execution import dhan_client
from models import ScalpGateState, ScalpPosition
from orders import overnight_stop
from screening import intraday_eligibility
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("position-stocks-eod")


class PositionAlreadyFlat(Exception):
    """Session 72: the overnight stop had already sold every remaining share
    (booked just before/after cancelling it), so no flat SELL is needed."""


def _place_overnight_stop(
    db: Session,
    pos: "ScalpPosition",
    stop_pct: float,
) -> Optional[str]:
    """Place a STOP_LOSS_MARKET SELL (CNC) immediately after the position
    has been converted from INTRADAY -> CNC.  Returns the confirmed Dhan
    order_id string on success, or None if placement/verification failed
    (caller then squares off the position rather than carrying it).

    stop_pct: percentage below entry_price to set the trigger (e.g. 4.0 for
    a 4% stop — trigger = entry_price * 0.96).

    This function only raises on programming errors (bad arguments); all
    Dhan-call failures are caught and returned as None so the caller can
    decide between squaring off or notifying and proceeding.
    """
    try:
        trigger = pos.entry_price * (1.0 - stop_pct / 100.0)
        if trigger <= 0:
            logger.error(
                "_place_overnight_stop: %s (id=%d) — computed trigger=%.4f <= 0 "
                "(entry=%.2f stop_pct=%.2f) — not placing stop.",
                pos.symbol, pos.id, trigger, pos.entry_price, stop_pct,
            )
            return None

        result = dhan_client.place_cnc_stop_loss_market(
            db,
            is_armed=True,
            security_id=pos.dhan_security_id,
            exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
            quantity=pos.quantity,
            trigger_price=trigger,
            tag=f"OVERNIGHT_STOP_{pos.id}",
        )
        order_id = str(result.get("orderId") or result.get("order_id") or "")
        if not order_id:
            logger.error(
                "_place_overnight_stop: %s (id=%d) — place_cnc_stop_loss_market "
                "returned no orderId (result=%r) — treating as failure.",
                pos.symbol, pos.id, result,
            )
            return None

        logger.warning(
            "_place_overnight_stop: %s (id=%d) — protective STOP_LOSS_MARKET "
            "placed and confirmed: order_id=%s trigger=₹%.2f (%.1f%% below "
            "entry=₹%.2f)",
            pos.symbol, pos.id, order_id,
            dhan_client.round_to_tick(trigger), stop_pct, pos.entry_price,
        )
        return order_id

    except Exception as e:
        logger.error(
            "_place_overnight_stop: %s (id=%d) — stop placement FAILED: %s",
            pos.symbol, pos.id, e, exc_info=True,
        )
        try:
            notifier.notify_critical(
                f"❌ <b>OVERNIGHT STOP PLACEMENT FAILED</b>\n"
                f"{pos.symbol} (id={pos.id}) converted to CNC but protective "
                f"STOP_LOSS_MARKET could not be confirmed live — squaring off "
                f"instead of leaving unprotected.\nError: {str(e)[:300]}"
            )
        except Exception:
            pass
        return None


def _run_edis_precheck(db: Session, positions: list, *, _edis_override: dict | None = None) -> None:
    """Check CDSL eDIS/TPIN status before the EOD squareoff loop executes
    CNC SELLs for overnight-converted positions (session72 fix — issue #17).

    Extracted into its own function so tests/test_edis_precheck.py can call
    it in isolation.  The _edis_override kwarg is for tests only — production
    callers must never pass it.

    Never raises: a failed eDIS check or a thrown exception only emits a log/
    notification; the SELL attempt is always made regardless of the outcome.
    """
    cnc_positions = [p for p in positions if getattr(p, "overnight_converted_to_cnc", False)]
    if not cnc_positions:
        return

    try:
        edis_summary = _edis_override if _edis_override is not None \
            else dhan_client.edis_verification_summary(db)

        if edis_summary.get("verified_today") is False:
            pending_symbols = edis_summary.get("pending_symbols") or []
            logger.warning(
                "EOD squareoff: %d position(s) are CNC-converted and require "
                "CDSL eDIS/TPIN approval before the CNC SELL can execute — "
                "pending holdings: %s. Open the Dhan app NOW to approve TPIN.",
                len(cnc_positions),
                pending_symbols,
            )
            try:
                notifier.notify_critical(
                    f"⚠️ <b>CDSL eDIS NOT APPROVED — CNC SELL MAY FAIL</b>\n"
                    f"{len(cnc_positions)} position(s) converted to CNC need "
                    f"eDIS/TPIN approval in the Dhan app before the flat SELL can execute.\n"
                    f"Symbols: {', '.join(p.symbol for p in cnc_positions)}\n"
                    f"Pending eDIS holdings: {pending_symbols}\n"
                    f"Open the Dhan app → Portfolio → Verify Holdings (TPIN) IMMEDIATELY."
                )
            except Exception as _ne:
                logger.warning("EOD squareoff: notify_critical for eDIS warning failed: %s", _ne)

        elif edis_summary.get("verified_today") is None:
            logger.warning(
                "EOD squareoff: eDIS verification check inconclusive for %d CNC position(s) "
                "(shape unrecognized or call failed — see edis_verification_summary note). "
                "Proceeding with flat SELL attempt anyway. Detail: %s",
                len(cnc_positions),
                edis_summary.get("detail", ""),
            )
        else:
            logger.info(
                "EOD squareoff: eDIS verified for %d CNC position(s) — CNC SELL should succeed.",
                len(cnc_positions),
            )
    except Exception as _edis_e:
        logger.warning(
            "EOD squareoff: eDIS pre-check failed (non-fatal, will attempt SELL anyway): %s",
            _edis_e,
        )


def _fire_flat_sell(db: Session, pos: ScalpPosition) -> dict:
    """2026-09-15 fix (session40): places the flat-SELL for one position,
    retrying up to config.EOD_SELL_RETRY_ATTEMPTS times ONLY when the
    failure doesn't match one of the three classified permanent-for-today
    rejection types below (those can never succeed on an immediate retry,
    so retrying them would just add pointless broker calls — same
    reasoning as real-trade-service's intraday-cutoff short-circuit).
    Everything else (a transient network blip, a momentary RMS hiccup) is
    the kind of failure a same-second retry can plausibly clear, and
    previously got exactly one attempt before the position was left OPEN
    until tomorrow.

    Returns the raw place_order() result dict (has ``orderId``) on
    success. Raises the last exception once attempts are exhausted, same
    as an unretried call would have — callers keep their existing
    try/except classification logic unchanged.

    AUDIT FIX (2026-09-19, overnight-hold product-type gap): this
    previously always sent product_type=config.SCALP_PRODUCT_TYPE
    ("INTRADAY"), with no awareness that a position could already be a
    real CNC holding. Once the overnight-carry feature existed, that
    became reachable for real: the stop_failed_positions fallback in
    run_eod_squareoff() (converted to CNC, but the protective stop failed
    to place, so it falls through to here to square off) and any manual
    exit of a still-open overnight-carried position both hit this exact
    path. An INTRADAY SELL against an actual CNC holding has no matching
    MIS position to net against and Dhan's RMS will very likely reject
    it — leaving the position both unprotected AND unflattened, exactly
    what the overnight-stop feature exists to prevent. Mirrors
    real-trade-service/exit_engine/exit.py's existing pattern of picking
    the SELL's product_type off the position's actual holding type rather
    than a single global constant.

    Also defensively cancels any still-live overnight_stop_order_id before
    firing this SELL — without that, a resting protective stop order and
    this independent flat SELL could both be live for the same quantity
    at once, risking a broker rejection (insufficient holding qty) or a
    double-sell. A missing/already-filled/already-cancelled stop is a
    no-op here, not an error.
    """
    if pos.overnight_converted_to_cnc and pos.overnight_stop_order_id:
        # session72 (open-issue #1): book any fill of the resting stop BEFORE
        # sizing the SELL — otherwise a partially-filled stop plus a full-size
        # flat SELL oversells — and AGAIN after the cancel to catch a fill that
        # landed in between. pos.quantity is then what is really left.
        _pre = overnight_stop.settle_before_flat_sell(db, pos)
        if _pre["closed"] or (pos.quantity or 0) <= 0:
            raise PositionAlreadyFlat(f"{pos.symbol} (id={pos.id}) fully sold by its overnight stop")
        try:
            dhan_client.cancel_cnc_stop_loss_order(db, order_id=pos.overnight_stop_order_id)
            logger.info(
                "_fire_flat_sell: %s (id=%d) — cancelled resting overnight stop "
                "%s before flat SELL.",
                pos.symbol, pos.id, pos.overnight_stop_order_id,
            )
        except Exception as e:
            logger.warning(
                "_fire_flat_sell: %s (id=%d) — failed to cancel resting overnight "
                "stop %s before flat SELL (may already be filled/cancelled elsewhere): %s",
                pos.symbol, pos.id, pos.overnight_stop_order_id, e,
            )
        _post = overnight_stop.settle_before_flat_sell(db, pos)
        if _post["closed"] or (pos.quantity or 0) <= 0:
            raise PositionAlreadyFlat(f"{pos.symbol} (id={pos.id}) fully sold by its overnight stop during cancel")
        overnight_stop.assign_stop_order(pos, None)  # caller commits pos shortly after

    sell_product_type = "CNC" if pos.overnight_converted_to_cnc else config.SCALP_PRODUCT_TYPE

    last_exc: Exception | None = None
    attempts = max(1, config.EOD_SELL_RETRY_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            return dhan_client.place_order(
                db,
                is_armed=True,
                security_id=pos.dhan_security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="SELL",
                quantity=pos.quantity,
                order_type="MARKET",
                price=0.0,
                product_type=sell_product_type,
                tag="EOD_SQUAREOFF",
            )
        except Exception as e:  # noqa: BLE001 — classified by the caller
            last_exc = e
            err_str = str(e)
            if (
                dhan_client.is_intraday_cutoff_error(err_str)
                or dhan_client.is_security_intraday_restricted_error(err_str)
                or dhan_client.is_insufficient_funds_error(err_str)
                or dhan_client.is_circuit_limit_error(err_str)
            ):
                # Permanent for today (or permanent, period) — no point
                # retrying, fail fast so the caller's classification/
                # logging runs immediately instead of after a pointless wait.
                raise
            if attempt < attempts:
                logger.warning(
                    "EOD squareoff: %s (id=%d) flat SELL attempt %d/%d failed "
                    "with an unclassified (possibly transient) error — retrying "
                    "in %.1fs: %s",
                    pos.symbol, pos.id, attempt, attempts,
                    config.EOD_SELL_RETRY_DELAY_SECONDS, err_str,
                )
                time.sleep(config.EOD_SELL_RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    # BUG FIX (session13, found via live testing): lazy reset of
    # gate.daily_loss_kill_switch_tripped — see main.py's
    # _maybe_lazy_reset_gate_kill_switch() for the full story.
    if row.daily_loss_kill_switch_tripped and row.daily_loss_kill_switch_tripped_date != ist_today_str():
        row.daily_loss_kill_switch_tripped = False
        row.daily_loss_kill_switch_tripped_date = None
        db.commit()
        logger.info("gate: lazy daily reset applied to gate.daily_loss_kill_switch_tripped")
    return row


def run_eod_squareoff(db: Session) -> int:
    """Close all OPEN scalp positions. Returns number of positions closed."""
    gate = _get_gate_state(db)
    today = ist_today_str()

    if gate.eod_squareoff_fired_date == today:
        logger.info("EOD squareoff already fired today (%s) — skipping", today)
        return 0

    # 2026-09-15 fix (session41b): also pick up EXIT_LEGS_REJECTED positions
    # (super-order exit legs were rejected — circuit-limit / surveillance).
    # Those positions cannot self-exit via their super order.  We attempt
    # ONE plain MARKET SELL here; if it also fails (e.g. stock is frozen at
    # lower circuit) we log and mark ERROR — we do NOT loop.  The once-per-
    # day guard (eod_squareoff_fired_date) prevents this from re-running.
    open_positions = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .all()
    )
    if not open_positions:
        logger.info("EOD squareoff: no open scalp positions to close")
        gate.eod_squareoff_fired_date = today
        db.commit()
        return 0

    logger.warning(
        "EOD squareoff: closing %d open/exit-rejected scalp position(s) at market price",
        len(open_positions),
    )

    # ── Overnight carry filter (2026-09-18, session67; conversion + pool
    # cap added 2026-09-18 user audit finding — see config.py's
    # OVERNIGHT_HOLD_ENABLED comment block for the full "why") ──────────────
    # When OVERNIGHT_HOLD_ENABLED, positions that pass ALL four overnight
    # quality conditions (in profit + stricter fund/tech/mcap scores) are
    # candidates to carry — but only actually carry if (a) an aggregate
    # pool-exposure cap has room, ranked by quality score, and (b) this
    # service can successfully convert them from INTRADAY to a real CNC
    # holding via Dhan's own /positions/convert before Dhan's own RMS
    # auto-squareoff cutoff. Anything that fails either check is squared
    # off normally — never left in an ambiguous or unprotected state. Only
    # EXIT_LEGS_REJECTED positions are always squaredoff regardless (their
    # bracket is dead and cannot protect them overnight).
    carry_positions: list = []
    if config.OVERNIGHT_HOLD_ENABLED:
        from models import ScalpCandidateLog
        from feed import ws_client as _ws
        squareoff_only: list = []
        carry_candidates: list = []  # (pos, quality_score) — before the pool cap
        for pos in open_positions:
            if pos.status == "EXIT_LEGS_REJECTED":
                squareoff_only.append(pos)
                continue
            # AUDIT FIX (2026-09-19, overnight-hold re-conversion gap): a
            # position can reach this loop already overnight_converted_to_cnc
            # =True — normally reconcile.py's overnight-stop check closes
            # these out once the stop triggers, but an OVERNIGHT_STOP_LOSS_PCT
            # =0 opt-out carry (no stop at all) or a stop whose re-arm failed
            # can still be sitting OPEN+CNC when this sweep runs again. Never
            # re-run the "is this a fresh INTRADAY position worth carrying"
            # evaluation on it (it's already carried, and convert_position()
            # would be called INTRADAY->CNC on something that's already CNC,
            # which Dhan would reject) — just square it off via the
            # CNC-aware _fire_flat_sell() below.
            if pos.overnight_converted_to_cnc:
                squareoff_only.append(pos)
                continue
            # Get live price for unrealized P&L check
            try:
                buf = _ws.get_tick_buffer(pos.symbol)
                ltp = buf[-1][1] if buf else None
            except Exception:
                ltp = None
            if ltp is None or ltp <= pos.entry_price:
                # Not in profit or no live price — squareoff
                squareoff_only.append(pos)
                continue
            # Check quality scores from entry-time candidate log
            log_row = (
                db.query(ScalpCandidateLog)
                .filter_by(symbol=pos.symbol, decision="ENTERED")
                .order_by(ScalpCandidateLog.created_at.desc())
                .first()
            )
            # Any None quality field = unknown = don't carry overnight
            if (
                log_row is None
                or log_row.fundamental_score is None
                or log_row.technical_score is None
                or log_row.market_cap_cr is None
                or log_row.fundamental_score < config.OVERNIGHT_MIN_FUNDAMENTAL_SCORE
                or log_row.technical_score < config.OVERNIGHT_MIN_TECHNICAL_SCORE
                or log_row.market_cap_cr < config.OVERNIGHT_MIN_MARKET_CAP_CR
            ):
                squareoff_only.append(pos)
                continue
            # Quality conditions passed — candidate for carry, subject to
            # the aggregate pool cap below.
            carry_candidates.append((pos, log_row.fundamental_score + log_row.technical_score))

        # ── Aggregate pool-exposure cap (2026-09-18 — user audit finding) ──
        # Individually-qualifying positions can still add up to an
        # unbounded share of the pool sitting exposed to overnight gap
        # risk. Rank by combined quality score (highest first) and keep
        # only as many as fit under OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL
        # of total_allocated_capital — same pattern real-trade-service
        # already uses for its own overnight-hold exposure cap.
        capped_out: list = []
        if carry_candidates:
            from capital import ledger as _ledger
            pool_state = _ledger.get_state(db)
            total_pool = float(pool_state.get("total_allocated_capital") or 0.0)
            cap_value = total_pool * (config.OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL / 100.0)
            carry_candidates.sort(key=lambda t: t[1], reverse=True)
            running_value = 0.0
            for pos, _score in carry_candidates:
                position_value = pos.capital_risked or (pos.entry_price * pos.quantity)
                if total_pool > 0 and (running_value + position_value) > cap_value:
                    capped_out.append(pos)
                    continue
                running_value += position_value
                carry_positions.append(pos)

        # ── Convert each surviving carry candidate INTRADAY -> CNC ─────────
        # This is what actually makes "carry" real — see config.py's
        # OVERNIGHT_HOLD_ENABLED comment for why skipping squareoff alone
        # (the old behavior) carried nothing at all.
        #
        # 2026-09-19 (option 3 fix): after each successful INTRADAY -> CNC
        # conversion, immediately place a STOP_LOSS_MARKET SELL (CNC,
        # product_type=CNC, trigger_price = entry_price * (1 -
        # OVERNIGHT_STOP_LOSS_PCT / 100)) via _place_overnight_stop() below.
        # That function verifies the order is live on Dhan's side (not just
        # accepted at the REST layer) before returning the order_id.
        # If placement or verification fails, the position is added to
        # conversion_failed and squared off — we never leave a CNC holding
        # overnight without a confirmed protective stop.
        converted_positions: list = []
        conversion_failed: list = []
        stop_failed_positions: list = []  # converted OK, stop placement failed
        for pos in carry_positions:
            try:
                if config.USE_SUPER_ORDER and pos.dhan_super_order_id:
                    for leg in ("TARGET_LEG", "STOP_LOSS_LEG"):
                        try:
                            dhan_client.cancel_super_order(
                                db, order_id=pos.dhan_super_order_id, order_leg=leg
                            )
                        except Exception:
                            pass  # leg may already be filled/cancelled — not fatal
                    wait_s = getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5)
                    if wait_s > 0:
                        time.sleep(wait_s)
                dhan_client.convert_position(
                    db,
                    is_armed=True,  # protective/exposure-reducing action, same policy as cancel/modify_super_order
                    security_id=pos.dhan_security_id,
                    exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                    position_type="LONG",  # this service is long-only (see orders/entry.py)
                    convert_qty=pos.quantity,
                    from_product_type=config.SCALP_PRODUCT_TYPE,
                    to_product_type="CNC",
                )
                pos.overnight_converted_to_cnc = True
                pos.dhan_super_order_id = None  # legs cancelled above; no longer applicable
                logger.info(
                    "EOD overnight carry: %s (id=%d) converted INTRADAY -> CNC, qty=%d",
                    pos.symbol, pos.id, pos.quantity,
                )

                # ── Place protective STOP_LOSS_MARKET immediately ──────────
                # If OVERNIGHT_STOP_LOSS_PCT is 0 or negative, skip stop
                # placement (operator opted out explicitly).
                stop_pct = getattr(config, "OVERNIGHT_STOP_LOSS_PCT", 4.0)
                if stop_pct > 0:
                    stop_order_id = _place_overnight_stop(db, pos, stop_pct)
                    if stop_order_id:
                        overnight_stop.assign_stop_order(pos, stop_order_id)
                        converted_positions.append(pos)
                    else:
                        # _place_overnight_stop() returned None — it already
                        # logged the failure reason.  Square off instead of
                        # leaving an unprotected CNC overnight.
                        logger.error(
                            "EOD overnight carry: %s (id=%d) — protective stop "
                            "placement failed (see above) — squaring off instead of "
                            "leaving an unprotected CNC holding overnight.",
                            pos.symbol, pos.id,
                        )
                        stop_failed_positions.append(pos)
                else:
                    # Operator set OVERNIGHT_STOP_LOSS_PCT=0 — no stop.
                    logger.warning(
                        "EOD overnight carry: %s (id=%d) — OVERNIGHT_STOP_LOSS_PCT=0, "
                        "carrying WITHOUT a protective stop (operator opt-out).",
                        pos.symbol, pos.id,
                    )
                    converted_positions.append(pos)

            except Exception as e:
                # Conversion failed (e.g. insufficient margin for full CNC
                # funding, or an API error) — fall back to squaring off.
                # Never assume success and leave this ambiguous.
                logger.warning(
                    "EOD overnight carry: %s (id=%d) conversion FAILED (%s) — "
                    "squaring off instead",
                    pos.symbol, pos.id, e,
                )
                conversion_failed.append(pos)

        squareoff_only.extend(capped_out)
        squareoff_only.extend(conversion_failed)
        squareoff_only.extend(stop_failed_positions)
        carry_positions = converted_positions
        db.commit()  # persist overnight_converted_to_cnc / overnight_stop_order_id /
                     # cleared dhan_super_order_id for carried positions, which don't
                     # pass through the squareoff loop below (that loop commits
                     # per-position itself).

        if carry_positions or capped_out or stop_failed_positions:
            from notifier import notify_sync
            lines = []
            if carry_positions:
                stop_pct = getattr(config, "OVERNIGHT_STOP_LOSS_PCT", 4.0)
                lines.append(
                    f"🌙 *EOD overnight carry — {len(carry_positions)} position(s) "
                    f"converted to CNC and held:*"
                )
                for p in carry_positions:
                    stop_level = round(p.entry_price * (1 - stop_pct / 100.0), 2) if stop_pct > 0 else None
                    stop_note = f" | stop ₹{stop_level:.2f} (order {p.overnight_stop_order_id})" if stop_level and p.overnight_stop_order_id else " | ⚠️ NO STOP (OVERNIGHT_STOP_LOSS_PCT=0)"
                    lines.append(f"  • {p.symbol} entry=₹{p.entry_price:.2f} qty={p.quantity}{stop_note}")
                if stop_pct > 0:
                    lines.append(
                        f"✅ Protective STOP_LOSS_MARKET orders placed and confirmed "
                        f"live on Dhan at {stop_pct:.1f}% below entry. "
                        f"Verify holdings (CDSL TPIN) in the Dhan app before "
                        f"market open so the stops can actually execute."
                    )
                else:
                    lines.append(
                        "⚠️ OVERNIGHT_STOP_LOSS_PCT=0 — NO protective stop placed. "
                        "Verify holdings (CDSL TPIN) in the Dhan app *before market "
                        "open* and be ready to manage these manually at the open."
                    )
            if stop_failed_positions:
                lines.append(
                    f"❌ {len(stop_failed_positions)} position(s) squared off because "
                    f"protective stop placement failed after CNC conversion: "
                    + ", ".join(p.symbol for p in stop_failed_positions)
                    + " — check logs for the specific stop-placement error."
                )
            if capped_out:
                lines.append(
                    f"ℹ️ {len(capped_out)} other qualifying position(s) squared off "
                    f"instead — pool-exposure cap "
                    f"({config.OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL:.0f}% of pool) reached: "
                    + ", ".join(p.symbol for p in capped_out)
                )
            notify_sync("\n".join(lines))
        open_positions = squareoff_only

    # FIX (session72 — issue #17): CNC-converted positions that are being
    # squared off (stop_failed_positions) require CDSL eDIS/TPIN authorization
    # in the Dhan app before a CNC SELL can execute on the same day. Without
    # this check, the flat SELL silently fails with an RMS rejection after the
    # position is already marked EOD_SQUAREOFF in the DB, leaving it
    # unflattened with no protective stop. We check once before the loop and
    # emit a critical alert so the operator can approve TPIN in time. We still
    # attempt the SELL either way — the broker may accept it if eDIS was
    # already pre-approved (or if it's already past T+1 settlement on a re-run).
    _run_edis_precheck(db, open_positions)

    closed = 0
    for pos in open_positions:
        try:
            if config.USE_SUPER_ORDER and pos.dhan_super_order_id:
                # Cancel all legs of the super order first, then close at market
                for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
                    try:
                        dhan_client.cancel_super_order(
                            db, order_id=pos.dhan_super_order_id, order_leg=leg
                        )
                    except Exception:
                        pass  # leg may already be filled/cancelled — not fatal

                # 2026-09-18 fix (this session): backport of session67's
                # close_position_now() fix. Without this pause, the plain
                # MARKET SELL fired below can race against a still-live
                # TARGET_LEG or STOP_LOSS_LEG on Dhan's side, resulting in a
                # rejected/double-fill or a position that appears closed in
                # our DB but still has a live exit leg sitting at the
                # broker. This is the highest-volume, least-supervised close
                # path in the service (can flatten up to
                # MAX_CONCURRENT_SCALP_POSITIONS positions unattended every
                # trading day), so it needs the same protection that was
                # already applied to manual/stagnation exits. Configurable
                # via MANUAL_EXIT_CANCEL_WAIT_S; default 0.5s; set to 0 to
                # restore the original no-wait behaviour.
                wait_s = getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5)
                if wait_s > 0:
                    time.sleep(wait_s)

            # Always place a plain MARKET SELL to guarantee flat.
            #
            # AUDIT FIX (this session): this previously passed
            # is_armed=gate.is_armed. dhan_client.place_order() raises
            # DhanNotArmedError whenever is_armed is False, with NO
            # exemption for SELL — unlike real-trade-service's risk engine
            # and manual_engine.py, which explicitly exempt SELL/exit
            # orders from the armed gate (see risk_engine/engine.py's
            # evaluate() docstring: "Exits must never be blocked..."). So a
            # disarmed service (e.g. after /disarm or /kill, or simply
            # never re-armed that morning) with open positions would hit
            # this exact EOD sweep, silently fail every closing SELL with
            # DhanNotArmedError (caught below, logged, position left OPEN),
            # and leave real-money positions unflattened past 3pm — exactly
            # the "no exceptions" scenario tracking doc §3.7 exists to rule
            # out. Forcing True here: flattening at EOD must never be
            # gated by the arm switch, same as cancel_order/
            # cancel_super_order already are unconditionally allowed above.
            #
            # 2026-09-15 fix (session40): routed through _fire_flat_sell()
            # for a small bounded retry on transient failures (see its
            # docstring), and the returned order id is now captured onto
            # pos.dhan_exit_order_id — previously nothing recorded which
            # broker order this SELL actually was, so reconcile.py's EOD
            # pending-reconcile pass had no way to look up its real fill
            # and was stuck using the entry_price placeholder forever.
            sell_result = _fire_flat_sell(db, pos)
            pos.dhan_exit_order_id = str(
                sell_result.get("orderId") or sell_result.get("order_id") or ""
            ) or None
            # Unconditional — tracked for visibility into the shared budget's
            # real usage, but never gates a forced exit (tracking doc §3.7
            # "no exceptions", applied to the shared guard too).
            shared_order_budget.record_order_unconditional(db)

            # Mark closed — exit price is unknown here (the MARKET SELL
            # just fired; Dhan has not yet confirmed a fill price). We record
            # entry_price as a placeholder so the row is immediately visible
            # in /positions as CLOSED, with a clear note that the final P&L
            # will be zero until reconciled. The next reconcile() pass that
            # reads Dhan's order book will overwrite exit_price / realized_pnl
            # with the real fill price once the MARKET SELL shows as TRADED.
            pos.status = "EOD_SQUAREOFF"
            pos.closed_at = datetime.now(timezone.utc)
            pos.exit_price = pos.entry_price   # placeholder — reconcile() will update
            # session72: keep P&L already booked from overnight-stop partials —
            # reconcile.py::_resolve_pending_with_price ADDS this SELL's P&L to it.
            _had_partials = ((pos.overnight_stop_filled_qty_so_far or 0) + (pos.overnight_stop_prior_qty or 0)) > 0
            pos.realized_pnl = (pos.realized_pnl or 0.0) if _had_partials else 0.0   # placeholder — reconcile() will update
            pos.realized_pnl_pct = (pos.realized_pnl_pct or 0.0) if _had_partials else 0.0
            # AUDIT FIX: record that this is a placeholder so operators
            # reading /positions or /trades/history before the next
            # reconcile() pass don't mistake 0.0 P&L for a real break-even
            # exit. The field is overwritten to None by reconcile() on a
            # real fill (it only sets error_message on REJECTED/CANCELLED).
            pos.error_message = "EOD_SQUAREOFF_PENDING_RECONCILE: exit_price=entry_price placeholder until next reconcile pass fills in the real fill price."
            db.commit()

            ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
            # AUDIT FIX (session60): flat SELL fired — release the symbol
            # lock now, not only on TARGET_HIT/STOP_HIT reconcile. The flat
            # SELL is a forced, unconditional exit (same as the shared
            # order-budget's "no exceptions" note above); leaving the lock
            # held until the next reconcile pass confirms the fill would
            # needlessly block a re-entry (by either service) for longer
            # than the position is actually still open.
            shared_symbol_lock.release(db, pos.symbol)
            closed += 1
            logger.info("EOD squareoff: closed %s (id=%d)", pos.symbol, pos.id)
        except PositionAlreadyFlat as _paf:
            logger.info("EOD squareoff: %s", _paf)
            closed += 1
            continue
        except Exception as e:
            err_str = str(e)
            # AUDIT FIX (2026-09-15): previously all SELL failures landed in
            # one generic catch — retried identically next cycle with no
            # diagnosis. Screenshots confirmed three distinct rejection types
            # firing from this service's own EOD sweep. Classify them:
            #
            # 1. INTRADAY_CUTOFF: "Intraday orders cannot be placed at this
            #    time" — the exchange window has closed for today. The position
            #    is not permanently stuck; tomorrow it's a delivery holding and
            #    CNC will work. Don't count against a permanent restriction.
            #
            # 2. SECURITY_INTRADAY_RESTRICTED: "not allowed to be traded in
            #    Intraday" — T2T/ASM/GSM surveillance stock. This SELL will
            #    NEVER succeed as INTRA on any future attempt. Record it so
            #    screening/_run_cycle() excludes it from future BUY candidates.
            #    The position is stranded until manual intervention or T+1
            #    settlement lets a CNC SELL clear.
            #
            # 3. INSUFFICIENT_FUNDS: Dhan's RMS treating an INTRA SELL as a
            #    new naked short because there is no matching MIS position to
            #    net against (common when the BUY itself was rejected but the
            #    EOD sweep still finds an OPEN DB row). Log clearly.
            #
            # 4. Everything else: generic failure — same log as before.
            if dhan_client.is_intraday_cutoff_error(err_str):
                logger.warning(
                    "EOD squareoff: %s (id=%d) — INTRADAY_CUTOFF: exchange window "
                    "closed for today. Position left OPEN; a CNC sell will be possible "
                    "tomorrow once settlement clears. Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INTRADAY_CUTOFF: {err_str}"
                # FIX (session70 audit): all SELL-failure branches now alert via
                # notify_critical — previously none of them did. A position that
                # fails to flatten at 3 PM has real capital at risk overnight with
                # its bracket already cancelled; silent log-only was the gap.
                try:
                    notifier.notify_critical(
                        f"⚠️ <b>EOD SQUAREOFF FAILED — INTRADAY CUTOFF</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Exchange window closed. CNC sell possible tomorrow.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_security_intraday_restricted_error(err_str):
                logger.error(
                    "EOD squareoff: %s (id=%d) — SECURITY_INTRADAY_RESTRICTED: "
                    "this symbol cannot be traded INTRA ever. Recording restriction "
                    "so future cycles skip it as a BUY candidate. Position left OPEN "
                    "— requires manual CNC sell or T+1 delivery settlement. Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INTRADAY_RESTRICTED: {err_str}"
                try:
                    intraday_eligibility.record_restriction(
                        db, pos.symbol,
                        detail=f"EOD SELL rejection: {err_str[:200]}",
                    )
                except Exception as rec_e:
                    logger.warning(
                        "EOD squareoff: could not record restriction for %s: %s",
                        pos.symbol, rec_e,
                    )
                try:
                    notifier.notify_critical(
                        f"🚫 <b>EOD SQUAREOFF FAILED — SURVEILLANCE RESTRICTED</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"T2T/ASM/GSM — cannot sell INTRA ever. Manual CNC sell required.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_insufficient_funds_error(err_str):
                logger.error(
                    "EOD squareoff: %s (id=%d) — INSUFFICIENT_FUNDS: Dhan RMS "
                    "margin shortfall on INTRA SELL (likely no matching MIS position "
                    "to net against — BUY may have been rejected but DB row persists). "
                    "Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_INSUFFICIENT_FUNDS: {err_str}"
                try:
                    notifier.notify_critical(
                        f"💸 <b>EOD SQUAREOFF FAILED — INSUFFICIENT FUNDS</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Dhan RMS margin shortfall on INTRA SELL (no matching MIS position?).\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            elif dhan_client.is_circuit_limit_error(err_str):
                # 2026-09-15 fix (session41b): stock is at lower circuit —
                # the SELL price is outside the allowed band.  Cannot fill
                # today.  Record it; do NOT retry.
                logger.error(
                    "EOD squareoff: %s (id=%d) — CIRCUIT_LIMIT: stock may be at "
                    "lower circuit; SELL price outside band.  Position left OPEN; "
                    "try a CNC sell tomorrow.  Error: %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_CIRCUIT_LIMIT: {err_str}"
                try:
                    notifier.notify_critical(
                        f"🔴 <b>EOD SQUAREOFF FAILED — CIRCUIT LIMIT</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Stock at lower circuit; SELL price outside band. Try CNC sell tomorrow.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            else:
                logger.error(
                    "EOD squareoff: failed to close %s (id=%d): %s",
                    pos.symbol, pos.id, err_str,
                )
                pos.error_message = f"EOD_SQUAREOFF_FAILED: {err_str}"
                try:
                    notifier.notify_critical(
                        f"❌ <b>EOD SQUAREOFF FAILED</b>\n"
                        f"{pos.symbol} (id={pos.id}) x{pos.quantity} left OPEN overnight.\n"
                        f"Unclassified error — check logs immediately.\n"
                        f"Error: {err_str[:300]}"
                    )
                except Exception as _ne:
                    logger.warning("EOD squareoff: notify_critical failed for %s: %s", pos.symbol, _ne)
            db.commit()

    gate.eod_squareoff_fired_date = today
    db.commit()
    logger.info("EOD squareoff: done — %d/%d closed", closed, len(open_positions))
    return closed


class ManualCloseRejected(Exception):
    """Raised by close_position_now() for a rejection the caller (a live
    admin request) should surface as an explicit error — same reasoning
    as entry.py's ManualEntryRejected."""


def close_position_now(db: Session, pos: ScalpPosition, exit_reason: str = "MANUAL_EXIT") -> dict:
    """Manual override — flatten ONE open scalp position immediately, any
    time of day, independent of whether its own bracket target/stop would
    currently trigger. This is the "manual exit if needed" feature (no
    such path existed before: the only ways a position ever closed were
    its own Super Order target/stop leg filling, or the once-a-day EOD
    sweep — there was no way for an admin to just get out of one position
    right now).

    Deliberately mirrors run_eod_squareoff()'s per-position body above
    (cancel every Super Order leg first — the entry side may still be
    resting for a MARKET-priced order that briefly hasn't filled, so this
    is a no-op-if-already-filled defensive cancel, same as EOD does — then
    a plain MARKET SELL to guarantee flat) rather than introducing a
    second, different close mechanism: same proven retry/error-
    classification path (_fire_flat_sell), same PENDING_RECONCILE
    placeholder convention that orders/reconcile.py already knows how to
    resolve once the real fill price is known (see that module's
    _reconcile_eod_pending — generalized this session to also handle
    status="MANUAL_EXIT", not just "EOD_SQUAREOFF").

    Always allowed regardless of the armed switch — exiting a position
    must never be gated by "not armed", same policy eod_squareoff.py's own
    forced-True is_armed already documents above, and the same policy
    real-trade-service's manual close route uses.

    Raises ManualCloseRejected with a human-readable reason if the
    position isn't in a closeable state or Dhan rejects the flat SELL.
    Returns a small status dict on success (the real fill/P&L is not yet
    known — same placeholder-then-reconcile flow as EOD squareoff).

    `exit_reason` (session68, made a REAL status this session): labels the
    error_message placeholder, the Telegram notification, AND pos.status
    itself — e.g. run_stagnation_exit() below passes "STAGNATION_EXIT" and
    the position is actually stored/shown as STAGNATION_EXIT, not
    MANUAL_EXIT. reconcile.py's _FLAT_SELL_PENDING_STATUSES tuple now
    includes "STAGNATION_EXIT" alongside "EOD_SQUAREOFF"/"MANUAL_EXIT" so
    the same pending-fill resolution path covers it — see that module.
    Previously this was cosmetic-only (error_message/notification said
    STAGNATION_EXIT but pos.status was hardcoded to the literal
    "MANUAL_EXIT"), which is exactly the mislabeling the user flagged:
    every stagnation early-exit showed up in Positions/Trade History
    indistinguishable from a real manual exit."""
    if pos.status not in ("OPEN", "EXIT_LEGS_REJECTED"):
        raise ManualCloseRejected(f"Position is {pos.status} — nothing to close.")

    if config.USE_SUPER_ORDER and pos.dhan_super_order_id:
        for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
            try:
                dhan_client.cancel_super_order(
                    db, order_id=pos.dhan_super_order_id, order_leg=leg
                )
            except Exception:
                pass  # leg may already be filled/cancelled — not fatal
        # 2026-09-18 fix (session67): give Dhan a moment to acknowledge the
        # cancellations before firing the SELL. Without this pause, the plain
        # MARKET SELL can race against a still-live TARGET_LEG or
        # STOP_LOSS_LEG on Dhan's side, resulting in a rejected/double-fill
        # or a position that appears closed in our DB but still has a live
        # exit leg sitting at the broker. Even 0.3–0.5s is enough for the
        # cancel to propagate over the RMS. Configurable via
        # MANUAL_EXIT_CANCEL_WAIT_S; default 0.5s; set to 0 to restore the
        # original no-wait behaviour.
        wait_s = getattr(config, "MANUAL_EXIT_CANCEL_WAIT_S", 0.5)
        if wait_s > 0:
            time.sleep(wait_s)

    try:
        sell_result = _fire_flat_sell(db, pos)
    except PositionAlreadyFlat as _paf:
        raise ManualCloseRejected(f"Already closed — {_paf}")
    except Exception as e:
        err_str = str(e)
        if dhan_client.is_intraday_cutoff_error(err_str):
            raise ManualCloseRejected(
                f"Dhan rejected — intraday order window has closed for today: {err_str}"
            )
        if dhan_client.is_security_intraday_restricted_error(err_str):
            try:
                intraday_eligibility.record_restriction(
                    db, pos.symbol, detail=f"Manual exit SELL rejection: {err_str[:200]}",
                )
            except Exception:
                pass
            raise ManualCloseRejected(
                f"Dhan rejected — {pos.symbol} is not tradeable intraday (T2T/ASM/GSM "
                f"surveillance): {err_str}"
            )
        if dhan_client.is_insufficient_funds_error(err_str):
            raise ManualCloseRejected(f"Dhan rejected — insufficient margin for the exit SELL: {err_str}")
        if dhan_client.is_circuit_limit_error(err_str):
            raise ManualCloseRejected(f"Dhan rejected — stock is at circuit limit: {err_str}")
        raise ManualCloseRejected(f"Dhan rejected the manual exit: {err_str}")

    pos.dhan_exit_order_id = str(
        sell_result.get("orderId") or sell_result.get("order_id") or ""
    ) or None
    shared_order_budget.record_order_unconditional(db)

    pos.status = exit_reason  # real status now, not a hardcoded literal — see exit_reason note above
    pos.closed_at = datetime.now(timezone.utc)
    pos.exit_price = pos.entry_price   # placeholder — reconcile() will update, same as EOD squareoff
    _had_partials = ((pos.overnight_stop_filled_qty_so_far or 0) + (pos.overnight_stop_prior_qty or 0)) > 0
    pos.realized_pnl = (pos.realized_pnl or 0.0) if _had_partials else 0.0   # session72: keep booked stop partials
    pos.realized_pnl_pct = (pos.realized_pnl_pct or 0.0) if _had_partials else 0.0
    pos.error_message = f"{exit_reason}_PENDING_RECONCILE: exit_price=entry_price placeholder until next reconcile pass fills in the real fill price."
    db.commit()

    ledger.release_capital(db, position_value=pos.capital_risked, realized_pnl=0.0)
    # AUDIT FIX (session60): same optimistic release as the EOD sweep above
    # — reconcile.py's dead-status branch re-claims it if this SELL turns
    # out to have died with zero fill.
    shared_symbol_lock.release(db, pos.symbol)
    logger.info("%s: closed %s (id=%d) — awaiting broker fill confirmation", exit_reason, pos.symbol, pos.id)

    from notifier import notify_sync
    _label = "Manual EXIT sent" if exit_reason == "MANUAL_EXIT" else f"{exit_reason.replace('_', ' ').title()} sent"
    notify_sync(
        f"📤 <b>{_label}</b> — {pos.symbol} x{pos.quantity}\n"
        f"Entry ₹{pos.entry_price:.2f} | Order {pos.dhan_exit_order_id or 'N/A'}\n"
        f"Awaiting broker fill confirmation."
    )

    return {"id": pos.id, "symbol": pos.symbol, "status": "pending_broker_confirmation"}


def run_stagnation_exit(db: Session) -> int:
    """session68/69. Toggle is DB-backed (ScalpGateState.stagnation_exit_
    enabled — POST /stagnation-exit/enable|disable, frontend button on the
    Pipeline tab), not config/env — see config.py's tuning-knobs comment
    for why. Closes an OPEN position early if it has moved less than
    config.STAGNATION_EXIT_BAND_PCT (either direction) from its entry
    price after config.STAGNATION_EXIT_MINUTES — i.e. neither its target
    nor its stop is anywhere close, and it isn't going to be. Frees that
    capital and its MAX_CONCURRENT_SCALP_POSITIONS slot for a better
    candidate the SAME session instead of parking it dead until the 15:00
    EOD sweep.

    Root cause this targets — 2026-09-17's trade history: MANGALAM,
    GEEKAYWIRE and JISLJALEQS all sat inside a near-zero P&L band the
    entire session and only ever closed via EOD_SQUAREOFF, while TREL
    (fund=49, tech=78 — a real candidate) repeatedly hit
    INSUFFICIENT_CAPITAL. Reusing close_position_now()'s proven cancel-
    wait-sell + PENDING_RECONCILE path rather than a second exit
    mechanism; tagged exit_reason="STAGNATION_EXIT", which close_position_
    now() now stores as the real pos.status (this session — it used to
    fall back to the hardcoded "MANUAL_EXIT" literal), so it's
    distinguishable from a true manual exit everywhere: dashboard, Trade
    History, notifications. Intentionally does NOT touch EXIT_LEGS_REJECTED
    positions — those are eod_squareoff's job, not this one's.
    """
    gate = _get_gate_state(db)
    if not gate.stagnation_exit_enabled:
        return 0

    now = datetime.now(timezone.utc)
    open_positions = db.query(ScalpPosition).filter(ScalpPosition.status == "OPEN").all()
    closed = 0
    for pos in open_positions:
        # AUDIT FIX (2026-09-19, overnight-hold / stagnation-exit collision):
        # a carried-overnight CNC position's opened_at is its ORIGINAL entry
        # time (yesterday or earlier), so age_min below is trivially >=
        # STAGNATION_EXIT_MINUTES the instant the market reopens — the only
        # thing stopping it from being closed as "stagnant" is whether price
        # has already moved STAGNATION_EXIT_BAND_PCT (default 0.35%) away
        # from that old entry_price within the first tick or two of trading,
        # which is easy to not clear right at the open. That would close out
        # a position specifically chosen the night before for its quality
        # and overnight profit, mislabel it STAGNATION_EXIT, and burn the
        # whole point of the carry decision, seconds into the new session.
        # This mechanism exists for same-day flat positions sitting dead
        # inside their bracket, not for one already under its own dedicated
        # overnight protective stop (reconcile.py's
        # _reconcile_overnight_stops) — never eligible here.
        if pos.overnight_converted_to_cnc:
            continue
        try:
            opened_at = as_aware(pos.opened_at)
        except Exception:
            continue
        age_min = (now - opened_at).total_seconds() / 60.0
        if age_min < config.STAGNATION_EXIT_MINUTES:
            continue

        try:
            from feed import ws_client
            buf = ws_client.get_tick_buffer(pos.symbol)
            ltp = buf[-1][1] if buf else None
        except Exception:
            ltp = None
        if ltp is None or ltp <= 0 or not pos.entry_price:
            continue  # no live price to judge stagnation by — leave it to EOD/target/stop

        pct_move = abs(ltp - pos.entry_price) / pos.entry_price * 100.0
        if pct_move >= config.STAGNATION_EXIT_BAND_PCT:
            continue  # moved meaningfully — target/stop logic already owns this case

        try:
            close_position_now(db, pos, exit_reason="STAGNATION_EXIT")
            closed += 1
            logger.info(
                "STAGNATION_EXIT: %s (id=%d) closed after %.0fm flat within ±%.2f%% "
                "(ltp=₹%.2f entry=₹%.2f) — freeing capital/slot",
                pos.symbol, pos.id, age_min, config.STAGNATION_EXIT_BAND_PCT, ltp, pos.entry_price,
            )
        except ManualCloseRejected as e:
            logger.info("STAGNATION_EXIT: %s (id=%d) skipped — %s", pos.symbol, pos.id, e)
        except Exception as e:
            logger.error(
                "STAGNATION_EXIT: %s (id=%d) unexpected error: %s", pos.symbol, pos.id, e, exc_info=True,
            )
    return closed