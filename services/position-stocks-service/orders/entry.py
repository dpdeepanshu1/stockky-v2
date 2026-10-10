"""
orders/entry.py — position entry logic.

Wires together: candidate → adaptive levels → capital sizing →
Dhan Super Order (or plain order fallback) → DB record.

Safety checks (in order):
  1. is_armed guard
  2. Max concurrent positions check
  3. Cross-service symbol lock
  4. Range/high-low hard gate — reject entries sitting at today's high
     (config.MAX_ENTRY_RANGE_POSITION) — see _range_gate_reject()
  4b. Price guard (2026-10-05) — reject a stale last tick, a live price more than
      config.ENTRY_MAX_SLIPPAGE_PCT above the signal price, or a stock already up
      more than config.MAX_DAY_GAIN_PCT on the day — see _price_guard_reject()
  5. Same-symbol re-entry guard — reject buying back a just-closed symbol
     within the cooldown unless price has genuinely pulled back
     (config.SYMBOL_REENTRY_COOLDOWN_MINUTES) — see _reentry_guard_reject()
  6. Capital reserve (also enforces daily loss kill switch)
  7. Dhan security_id resolution
  8. Quantity computation (position_value / current_ltp, min 1)
  9. FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE → force qty=1 on first-ever order
     (group 218: otherwise the cost gate, orders/cost_gate.py, runs on the final quantity)
  10. Super Order placement (or plain MARKET fallback)
  11. DB record write
  12. Candidate log write
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
from models import ScalpCandidateLog, ScalpGateState, ScalpPosition
from orders import cost_gate, depth_gate, entry_pause
from orders.adaptive import AdaptiveLevels, compute as compute_levels
from screening import intraday_eligibility, opening_gate
from screening.engine import Candidate
from screening.quality_gate import QualitySignal
from screening import trade_gates
from tz_utils import as_aware, ist_today_str

logger = logging.getLogger("position-stocks-entry")


def _range_gate_reject(symbol: str, current_ltp: float) -> Optional[str]:
    """Hard price/high-low-awareness gate (this session).

    screening/engine.py's range_mult and orders/adaptive.py's near-high
    adjustment both already know about today's intraday range, but they
    only ever SOFTEN a candidate (lower score, tighter target) — nothing
    actually stopped a buy from going through when the stock is sitting
    right at its high for the day, which is the exact "buy on the high
    point" pattern flagged this session. Reject outright when LTP is
    within the top (1 - MAX_ENTRY_RANGE_POSITION) of today's observed
    tick range, but only once there's enough real tick depth
    (MIN_TICKS_FOR_RANGE_GATE) to trust that range — thin/early-session
    data fails OPEN (never rejects), matching every other gate here.
    Returns a skip reason string, or None if the entry is allowed.
    """
    try:
        from feed import ws_client
        # 2026-10-05: prefer the exchange's own day high/low (mode-3 feed). The
        # tick buffer only spans ~65 minutes, so the range computed from it is the
        # last hour's range, not the day's. Falls back to the buffer (needing
        # MIN_TICKS_FOR_RANGE_GATE ticks) when the exchange range is unknown.
        _rng_fn = getattr(ws_client, "get_day_range", None)
        _rng = _rng_fn(symbol) if _rng_fn else None
        if _rng:
            day_low, day_high = _rng
        else:
            buf = ws_client.get_tick_buffer(symbol)
            prices = [p for _t, p in buf if p > 0]
            if len(prices) < config.MIN_TICKS_FOR_RANGE_GATE:
                return None
            day_low, day_high = min(prices), max(prices)
        span = day_high - day_low
        if span <= 1e-6:
            return None
        range_pos = max(0.0, min(1.0, (current_ltp - day_low) / span))
        if range_pos >= config.MAX_ENTRY_RANGE_POSITION:
            return (
                f"NEAR_DAY_HIGH:range_pos={range_pos:.2f} "
                f">= floor={config.MAX_ENTRY_RANGE_POSITION:.2f} "
                f"(day_low={day_low:.2f} day_high={day_high:.2f})"
            )
        return None
    except Exception as e:
        logger.debug("entry: range gate check failed for %s: %s", symbol, e)
        return None


def _stale_tick_message(age_s: float, max_age_s: float) -> str:
    return f"STALE_TICK:last tick {age_s:.0f}s old > {max_age_s:.0f}s — current price unknown"


def _stale_tick_reject(symbol: str) -> Optional[str]:
    """group279: the stale-tick half of _price_guard_reject(), on its own, for the manual BUY path.

    The manual BUY route takes its price from ws_client.get_last_ltp(), which has no age, so a feed that went quiet
    minutes ago still looked like a live price. Same limit as the automatic path (ENTRY_MAX_TICK_AGE_S, 0 = off).
    Fails OPEN on a missing tick buffer or any error, like every other gate in this module (the route already
    refuses when there is no price at all)."""
    try:
        max_age = float(config.ENTRY_MAX_TICK_AGE_S)
        if max_age <= 0:
            return None
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if not buf:
            return None
        last_ts, last_px = buf[-1]
        if not last_px or last_px <= 0:
            return None
        age = time.time() - float(last_ts)
        if age > max_age:
            return _stale_tick_message(age, max_age)
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("entry: stale tick check failed for %s: %s", symbol, e)
        return None


def _price_guard_reject(symbol: str, signal_ltp: float) -> Optional[str]:
    """Pre-order price guards (2026-10-05, scalp review). Returns a skip reason
    or None.

    1. Stale tick: the last tick for the symbol is older than
       ENTRY_MAX_TICK_AGE_S, so the current price is unknown.
    2. Slippage: the entry is a MARKET order sent seconds after the scan tick.
       UNITEDPOLY signalled at 44.58 and filled at 48.14 (+8%). Re-read the live
       tick now and reject if it is more than ENTRY_MAX_SLIPPAGE_PCT above the
       signal price.
    3. Day gain: reject a stock already up more than MAX_DAY_GAIN_PCT vs the
       exchange's previous close.

    Every check fails OPEN (returns None) on missing data or an error, like the
    other gates here. A setting of 0 disables that check."""
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        live = None
        if buf:
            last_ts, last_px = buf[-1]
            if last_px and last_px > 0:
                live = float(last_px)
                max_age = config.ENTRY_MAX_TICK_AGE_S
                age = time.time() - float(last_ts)
                if max_age > 0 and age > max_age:
                    return _stale_tick_message(age, max_age)
        if live is not None and signal_ltp > 0 and config.ENTRY_MAX_SLIPPAGE_PCT > 0:
            slip = (live - signal_ltp) / signal_ltp * 100.0
            if slip > config.ENTRY_MAX_SLIPPAGE_PCT:
                return (
                    f"ENTRY_SLIPPAGE:live ₹{live:.2f} is {slip:.2f}% above signal "
                    f"₹{signal_ltp:.2f} (max {config.ENTRY_MAX_SLIPPAGE_PCT:.2f}%)"
                )
        if config.MAX_DAY_GAIN_PCT > 0:
            _ds_fn = getattr(ws_client, "get_day_stats", None)
            _ds = _ds_fn(symbol) if _ds_fn else None
            prev_close = _ds[3] if _ds else None
            ref = live if live is not None else signal_ltp
            if prev_close and prev_close > 0 and ref > 0:
                gain = (ref - prev_close) / prev_close * 100.0
                if gain > config.MAX_DAY_GAIN_PCT:
                    return (
                        f"DAY_GAIN_TOO_HIGH:up {gain:.2f}% vs prev close ₹{prev_close:.2f} "
                        f"(max {config.MAX_DAY_GAIN_PCT:.2f}%)"
                    )
        return None
    except Exception as e:
        logger.debug("entry: price guard check failed for %s: %s", symbol, e)
        return None


def _rejected_entry_reject(db: Session, symbol: str) -> Optional[str]:
    """Skip a symbol whose entry order Dhan has just rejected/cancelled (group192, HEGAM).

    A Super Order is accepted by Dhan's API and rejected a moment later by RMS, so attempt_entry() returned
    "success" and the dead entry only surfaced in orders/reconcile.py, which marks the row ERROR with an
    "Entry leg ..." message and frees the symbol again. The re-entry guard below ignores such rows (no exit
    price), so the symbol was re-bought on every cycle: 13 identical rejected orders in four minutes.

    Blocks the symbol for ENTRY_REJECT_COOLDOWN_MINUTES after the latest dead entry, and for the rest of
    the IST day once it has ENTRY_REJECT_MAX_PER_SYMBOL_DAY of them. Fails open on any DB error.
    """
    cooldown = config.ENTRY_REJECT_COOLDOWN_MINUTES
    max_day = config.ENTRY_REJECT_MAX_PER_SYMBOL_DAY
    if cooldown <= 0 and max_day <= 0:
        return None
    try:
        rows = (
            db.query(ScalpPosition)
            .filter(ScalpPosition.symbol == symbol)
            .filter(ScalpPosition.status == "ERROR")
            .filter(ScalpPosition.error_message.like("Entry leg%"))
            .filter(ScalpPosition.closed_at.isnot(None))
            .order_by(ScalpPosition.closed_at.desc())
            .all()
        )
        now = datetime.now(timezone.utc)
        today = ist_today_str(now)
        today_rows = [r for r in rows if (c := as_aware(r.closed_at)) is not None and ist_today_str(c) == today
                      # group209: a margin rejection is the account's problem, handled by the global pause in
                      # orders/entry_pause.py — it must not lock this symbol out for 30 min / the day.
                      and not dhan_client.is_insufficient_funds_error(r.error_message or "")]
        if not today_rows:
            return None
        if max_day > 0 and len(today_rows) >= max_day:
            return (
                f"ENTRY_REJECTED_TODAY:{len(today_rows)} entry order(s) for {symbol} were rejected/cancelled by "
                f"Dhan today (limit {max_day}) — skipped for the rest of the day"
            )
        last = as_aware(today_rows[0].closed_at)
        elapsed = (now - last).total_seconds() / 60.0
        if cooldown > 0 and elapsed < cooldown:
            return (
                f"ENTRY_REJECT_COOLDOWN:entry for {symbol} was rejected/cancelled {elapsed:.1f}m ago "
                f"({(today_rows[0].error_message or '')[:120]}) — waiting {cooldown}m"
            )
        return None
    except Exception as e:
        logger.warning("entry: rejected-entry check failed for %s (fail-open): %s", symbol, e)
        return None


def _reentry_guard_reject(db: Session, symbol: str, current_ltp: float) -> Optional[str]:
    """Same-symbol re-entry guard (this session).

    Finds the most recently CLOSED position for this symbol. If it closed
    within SYMBOL_REENTRY_COOLDOWN_MINUTES, only allows a fresh entry when
    current_ltp has pulled back at least SYMBOL_REENTRY_MIN_PULLBACK_PCT
    below that exit price — i.e. this is a genuine new dip, not the same
    move being chased a second time at/above the price we just sold at
    (the NAHARINDUS pattern: sell at ₹139.79, buy back 7 minutes later at
    ₹139.50, then stop out). No prior closed trade, or cooldown already
    elapsed, or a real pullback present → allowed (returns None).
    """
    # 2026-10-02: a symbol that already closed at a LOSS today is blocked for
    # the rest of the day — the 30-minute cooldown below let the system buy
    # back into the same fading move (GANDHAR, SUPREMEINF re-entered 2-3x).
    if config.SYMBOL_BLOCK_AFTER_LOSS_TODAY:
        lost = trade_gates.symbol_lost_today(db, symbol)
        if lost is not None:
            return (
                f"REENTRY_BLOCKED_AFTER_LOSS:{symbol} already closed at a loss today "
                f"({lost.status}, ₹{(lost.realized_pnl or 0.0):.2f}) — no re-entry until tomorrow"
            )

    last = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.symbol == symbol)
        .filter(ScalpPosition.status.notin_(("OPEN", "EXIT_LEGS_REJECTED")))
        .filter(ScalpPosition.closed_at.isnot(None))
        .order_by(ScalpPosition.closed_at.desc())
        .first()
    )
    if last is None or last.exit_price is None:
        return None

    closed_at = as_aware(last.closed_at)
    if closed_at is None:
        return None

    elapsed_minutes = (datetime.now(timezone.utc) - closed_at).total_seconds() / 60.0
    if elapsed_minutes >= config.SYMBOL_REENTRY_COOLDOWN_MINUTES:
        return None

    pullback_floor = last.exit_price * (1 - config.SYMBOL_REENTRY_MIN_PULLBACK_PCT / 100.0)
    if current_ltp <= pullback_floor:
        # Real dip below where we last exited — a legitimately new setup.
        return None

    return (
        f"REENTRY_COOLDOWN:closed {elapsed_minutes:.1f}m ago @ ₹{last.exit_price:.2f} "
        f"({last.status}), now ₹{current_ltp:.2f} hasn't pulled back "
        f"{config.SYMBOL_REENTRY_MIN_PULLBACK_PCT:.1f}% "
        f"(needs <= ₹{pullback_floor:.2f}, cooldown {config.SYMBOL_REENTRY_COOLDOWN_MINUTES}m)"
    )


def _get_gate_state(db: Session) -> ScalpGateState:
    row = db.query(ScalpGateState).filter_by(mode="REAL").first()
    if row is None:
        row = ScalpGateState(mode="REAL")
        db.add(row)
        db.commit()
        db.refresh(row)
    # BUG FIX (session13, found via live testing): lazy reset of
    # gate.daily_loss_kill_switch_tripped — see main.py's
    # _maybe_lazy_reset_gate_kill_switch() for the full story. This is the
    # copy that actually matters here: it's what the early-exit check just
    # below reads.
    if row.daily_loss_kill_switch_tripped and row.daily_loss_kill_switch_tripped_date != ist_today_str():
        row.daily_loss_kill_switch_tripped = False
        row.daily_loss_kill_switch_tripped_date = None
        db.commit()
        logger.info("gate: lazy daily reset applied to gate.daily_loss_kill_switch_tripped")
    return row


def _count_open_positions(db: Session) -> int:
    # BUG FIX (audit follow-up to session41b/42): only counted status="OPEN",
    # so an EXIT_LEGS_REJECTED position (still holding real capital and real
    # exposure — its exit legs were rejected, not its entry) was invisible to
    # the MAX_CONCURRENT_SCALP_POSITIONS gate below. That let the service
    # open MORE concurrent positions than the configured cap whenever a
    # stuck position existed, since it didn't count against the limit.
    return (
        db.query(ScalpPosition)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .count()
    )


def _log_candidate(
    db: Session,
    candidate: Candidate,
    decision: str,
    reason: str,
    composite_score: Optional[float] = None,
    quality: Optional[QualitySignal] = None,
) -> None:
    log = ScalpCandidateLog(
        symbol=candidate.symbol,
        window_source=candidate.window_label,
        pct_change=candidate.pct_change,
        composite_score=composite_score or candidate.composite_score,
        decision=decision,
        reason=reason,
        fundamental_score=quality.fundamental_score if quality else None,
        technical_score=quality.technical_score if quality else None,
        market_cap_cr=quality.market_cap_cr if quality else None,
        has_positive_catalyst=quality.has_positive_catalyst if quality else None,
    )
    db.add(log)
    db.commit()


def log_quality_reject(db: Session, candidate: Candidate, quality: QualitySignal, reason: str) -> None:
    """Record a candidate skipped by the quality gate BEFORE attempt_entry
    was even called (main.py's trading loop checks quality for the top-N
    candidates first) — keeps the audit trail (ScalpCandidateLog) complete
    for candidates that never reached the capital/Dhan checks inside
    attempt_entry."""
    _log_candidate(db, candidate, "SKIPPED", f"QUALITY_GATE:{reason}", quality=quality)


def attempt_entry(
    db: Session,
    candidate: Candidate,
    quality: Optional[QualitySignal] = None,
) -> Optional[ScalpPosition]:
    """Try to enter a position for the given candidate.
    Returns the ScalpPosition if entered, None otherwise (also logs why).
    `quality` (added session 6) is the best-effort fundamental/technical/
    news signal from screening/quality_gate.py, already checked by the
    caller — passed through here purely so it's recorded on the
    ScalpCandidateLog row alongside the entry/skip decision, for a full
    audit trail of what was known about a symbol at decision time."""

    gate = _get_gate_state(db)

    if not gate.is_armed:
        _log_candidate(db, candidate, "SKIPPED", "SERVICE_NOT_ARMED", quality=quality)
        return None

    if gate.daily_loss_kill_switch_tripped:
        _log_candidate(db, candidate, "SKIPPED", "DAILY_LOSS_KILL_SWITCH", quality=quality)
        return None

    # Order budget guard
    today = ist_today_str()
    if gate.orders_placed_today_date == today and gate.orders_placed_today >= config.DAILY_ORDER_BUDGET:
        _log_candidate(db, candidate, "SKIPPED", f"ORDER_BUDGET_EXHAUSTED:{gate.orders_placed_today}", quality=quality)
        return None

    # group209 (item 15): a recent margin rejection pauses every new entry; a recent placement failure
    # rests that one symbol. Checked before the symbol lock so nothing is claimed and released needlessly.
    _pause = entry_pause.all_paused() or entry_pause.symbol_blocked(candidate.symbol)
    if _pause:
        _log_candidate(db, candidate, "SKIPPED", _pause, quality=quality)
        return None

    # Max concurrent positions
    open_count = _count_open_positions(db)
    if open_count >= config.MAX_CONCURRENT_SCALP_POSITIONS:
        _log_candidate(db, candidate, "SKIPPED", f"MAX_POSITIONS:{open_count}", quality=quality)
        return None

    # group298: this service has no averaging-in, and try_claim() below lets a symbol this service already holds through
    # ("already ours"). The scan excludes open symbols, but only from a snapshot taken at the start of the cycle: a manual
    # BUY (it runs outside the cycle lock) or anything else that opened the symbol since then slipped past it. The manual
    # path has had this check since 2026-09-18; the automatic path now has it too, checked live.
    _held_here = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.symbol == candidate.symbol)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .first()
    )
    if _held_here is not None:
        _log_candidate(db, candidate, "SKIPPED", f"ALREADY_OPEN_HERE:id={_held_here.id},status={_held_here.status}",
                       quality=quality)
        return None

    # AUDIT FIX (session60): cross-service symbol lock — this service and
    # real-trade-service share one Dhan account, which holds a single
    # consolidated position per symbol with no concept of which service's
    # shares are whose. Checked here, before any capital is reserved,
    # because it's a cheap DB read and a duplicate-symbol buy should be
    # skipped as early as possible, not discovered after capital's already
    # committed. See capital/shared_symbol_lock.py for full context
    # (confirmed cause of the AEGISVOPAK broker order-type mismatch).
    if not shared_symbol_lock.try_claim(db, candidate.symbol):
        _log_candidate(db, candidate, "SKIPPED", "SYMBOL_HELD_BY_OTHER_SERVICE", quality=quality)
        return None

    # 2026-09-18 fix (session67): minimum stock price gate — reject penny
    # stocks before any capital is reserved or network calls are made.
    # Root cause of 5 FILATFASH ERROR rows: ₹0.19/share clears pct-change
    # and volume thresholds easily (1 paise = 5% move), but Dhan rejects
    # the order and the quality gate only runs afterward. Check price first.
    if candidate.current_ltp < config.MIN_STOCK_PRICE:
        _log_candidate(
            db, candidate, "SKIPPED",
            f"PENNY_STOCK:ltp=₹{candidate.current_ltp:.2f} < floor=₹{config.MIN_STOCK_PRICE:.2f}",
            quality=quality,
        )
        shared_symbol_lock.release(db, candidate.symbol)
        return None

    # AUDIT FIX (this session): hard high/low-awareness gate — reject a
    # candidate sitting right at today's high outright, before any capital
    # is committed. See _range_gate_reject()'s docstring.
    range_reject = _range_gate_reject(candidate.symbol, candidate.current_ltp)
    if range_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        # Deliberately its own "RANGE_GATE:" prefix, not "QUALITY_GATE:" —
        # the dashboard's Quality Gate panel's floor text describes the
        # fundamental/technical/market-cap floor specifically
        # (config.MIN_FUNDAMENTAL_SCORE etc.); mixing this in under that
        # label would misattribute the reject reason to the wrong gate.
        _log_candidate(db, candidate, "SKIPPED", f"RANGE_GATE:{range_reject}", quality=quality)
        return None

    # group 268: opening-quality gate (09:15 until OPENING_GATE_SETTLE_IST). Fails CLOSED while active.
    opening_reject = opening_gate.reject_reason(candidate.symbol, candidate.current_ltp)
    if opening_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", opening_reject, quality=quality)
        return None
    # group 269: shadow mode - the symbol passed the gate but no order is placed inside the window; log it once a day.
    if opening_gate.shadow_active():
        shared_symbol_lock.release(db, candidate.symbol)
        _sh_levels = compute_levels(candidate.pct_change, candidate.current_ltp, symbol=candidate.symbol)
        _sh_stop = opening_gate.stop_reject(candidate.symbol, _sh_levels.stop_pct)
        if _sh_stop:                      # shadow mode applies the stop check too, so WOULD_ENTER means every check passed
            _log_candidate(db, candidate, "SKIPPED", _sh_stop, quality=quality)
            return None
        if opening_gate.shadow_first_time(candidate.symbol):
            _log_candidate(db, candidate, "SKIPPED",
                           f"OPENING_SHADOW:WOULD_ENTER ltp={candidate.current_ltp:.2f}"
                           f"{opening_gate.entry_features(candidate.symbol, candidate.current_ltp)}"
                           # group 271: the levels a real entry would have used, so the row can be judged against later prices
                           f" stop={_sh_levels.stop_price:.2f}({_sh_levels.stop_pct:.2f}%)"
                           f" target={_sh_levels.target_price:.2f}({_sh_levels.target_pct:.2f}%)",
                           quality=quality)
        return None

    # 2026-10-05: stale-tick / slippage / day-gain guard — see _price_guard_reject().
    price_reject = _price_guard_reject(candidate.symbol, candidate.current_ltp)
    if price_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", f"PRICE_GUARD:{price_reject}", quality=quality)
        return None

    # AUDIT FIX (this session): same-symbol re-entry guard — reject buying
    # back into a symbol we just closed at/above the price we exited at,
    # within the cooldown window. See _reentry_guard_reject()'s docstring.
    reentry_reject = _rejected_entry_reject(db, candidate.symbol) or _reentry_guard_reject(
        db, candidate.symbol, candidate.current_ltp
    )
    if reentry_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", reentry_reject, quality=quality)
        return None

    # Compute adaptive levels — pass symbol so range-aware adjustment can
    # read today's intraday high/low from the live tick buffer (session41b).
    levels: AdaptiveLevels = compute_levels(
        candidate.pct_change, candidate.current_ltp, symbol=candidate.symbol
    )

    # group 270: inside the opening-gate window a stop tighter than a share of the daily ATR is noise-sized - skip.
    _stop_reject = opening_gate.stop_reject(candidate.symbol, levels.stop_pct)
    if _stop_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", _stop_reject, quality=quality)
        return None

    # group280 (plan C3): Dhan 5-level depth - skip a wide spread / thin book before any capital is reserved.
    _depth_reject = depth_gate.reject_reason(candidate.symbol)
    if _depth_reject:
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", _depth_reject, quality=quality)
        return None

    # Reserve capital (also checks kill switch again in the ledger)
    position_value = ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct)
    if position_value is None:
        _log_candidate(db, candidate, "SKIPPED", "INSUFFICIENT_CAPITAL", quality=quality)
        # FIX (session70): raise instead of returning None so _run_cycle()
        # can fall through to the next quality-passing candidate — the pool
        # may afford a cheaper stock even when it can't afford this one.
        # Release the symbol lock first — same as every other early-return
        # skip path above; leaving it held would block the next cycle from
        # even considering this symbol as a candidate.
        shared_symbol_lock.release(db, candidate.symbol)
        raise InsufficientCapitalSkip(
            f"{candidate.symbol}: INSUFFICIENT_CAPITAL — pool cannot size a position"
        )

    # Resolve Dhan security_id
    try:
        security_id = dhan_client.get_security_id(db, candidate.symbol)
    except dhan_client.SecurityNotResolvedError as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", f"SECURITY_NOT_FOUND:{e}", quality=quality)
        return None
    except Exception:
        # Any other lookup failure (Dhan not connected, scrip-master download error, ...) must not
        # leave the ledger reservation and the cross-service symbol lock held forever.
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, candidate.symbol)
        raise

    # Quantity
    raw_qty = int(position_value / candidate.current_ltp)
    quantity = max(1, raw_qty)
    # group283 (plan C3 size-down): do not let the order eat its own touch (off unless ENTRY_BOOK_MAX_SHARE_PCT > 0).
    # group286: then the same against Dhan's 20-level ask book within ENTRY_DEPTH20_SLIP_PCT of the touch (off unless
    # ENTRY_DEPTH20_SLIP_PCT > 0). The second cap sees the quantity left after the first. Both fail open.
    for _cap_fn, _cap_why in (
        (lambda: depth_gate.max_qty_from_book(candidate.symbol, candidate.current_ltp), "best-5 book cap"),
        (lambda: depth_gate.max_qty_from_depth20(candidate.symbol, quantity), "20-level book cap"),
    ):
        _cap_qty = _cap_fn()
        if _cap_qty is not None and quantity > _cap_qty:
            _new_value = _cap_qty * candidate.current_ltp
            logger.info("position-stocks entry: %s sized down %d -> %d shares (%s)",
                        candidate.symbol, quantity, _cap_qty, _cap_why)
            if _new_value < position_value:
                # hand the unused part of the reservation back so the pool does not sit on idle capital
                ledger.release_capital(db, position_value=position_value - _new_value, realized_pnl=0.0)
                position_value = _new_value
            quantity = _cap_qty

    is_first = not gate.first_live_order_done
    if is_first and config.FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE:
        logger.warning(
            "position-stocks: FIRST LIVE SUPER ORDER — forcing qty=1 (was %d) "
            "to observe Dhan's actual response. Disable FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE "
            "once you've confirmed a clean fill.", quantity,
        )
        quantity = 1
    else:
        # group 218 (review item 5): skip an entry whose target cannot pay for its own round-trip costs. Checked with
        # the quantity that will really trade; the forced 1-share first live order above is a deliberate probe and
        # is not gated. See orders/cost_gate.py (off with SCALP_COST_GATE_ENABLED=0).
        _cost_reject = cost_gate.reject_reason(candidate.current_ltp, quantity, levels.target_pct)
        if _cost_reject:
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            shared_symbol_lock.release(db, candidate.symbol)
            _log_candidate(db, candidate, "SKIPPED", f"COST_GATE:{_cost_reject}", quality=quality)
            return None

    # BUG FIX (this session): quantity is floored to at least 1 share above
    # (and may be forced to exactly 1 by the first-live-order override) —
    # but reserve_capital() above only ever deducted the risk-SIZED
    # `position_value` from the ledger, which can be SMALLER than what
    # `quantity` shares actually cost (a small pool and/or a high-priced
    # stock). Placing the real order anyway would spend more real rupees
    # than the ledger ever accounted for, silently overstating
    # available_capital afterwards — and since this pool is a
    # software-enforced half of one real, shared Dhan account, that
    # unaccounted overspend can eat into real-trade-service's half without
    # either ledger reflecting it. Reserve the real shortfall (if any)
    # before placing the order; skip the entry if even that isn't
    # available rather than place an order the ledger can't actually back.
    actual_cost = quantity * candidate.current_ltp
    if actual_cost > position_value:
        shortfall = actual_cost - position_value
        if not ledger.reserve_additional(db, shortfall):
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            shared_symbol_lock.release(db, candidate.symbol)
            _log_candidate(
                db, candidate, "SKIPPED",
                f"INSUFFICIENT_CAPITAL_FOR_MIN_QTY:shortfall={shortfall:.2f}",
                quality=quality,
            )
            # FIX (session70): same as INSUFFICIENT_CAPITAL above — raise so
            # _run_cycle() falls through to a cheaper candidate.
            raise InsufficientCapitalSkip(
                f"{candidate.symbol}: INSUFFICIENT_CAPITAL_FOR_MIN_QTY shortfall=₹{shortfall:.2f}"
            )
        position_value = actual_cost

    # Shared cross-service Dhan account-wide order-rate guard (tracking doc
    # §3.8) — checked here, right before the real Dhan call, not earlier:
    # everything above this point (max positions, capital, security
    # resolution) can still reject a candidate for reasons that have
    # nothing to do with order-rate, and none of those should count against
    # the shared budget. Separate from this service's OWN order budget
    # checked above — Dhan's real account-wide cap is shared with
    # real-trade-service too. Fails open on any error (see
    # shared_order_budget.py's docstring).
    if not shared_order_budget.check_and_reserve(db):
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, candidate.symbol)
        _log_candidate(db, candidate, "SKIPPED", "SHARED_ORDER_BUDGET_EXHAUSTED", quality=quality)
        return None

    # Place the order
    dhan_super_order_id = None
    plain_order_id = None
    error_msg = None
    try:
        if config.USE_SUPER_ORDER:
            result = dhan_client.place_super_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=candidate.current_ltp,   # entry reference price
                target_price=levels.target_price,
                stop_loss_price=levels.stop_price,
                trailing_jump=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="SCALP",
            )
            dhan_super_order_id = str(result.get("orderId") or result.get("id") or "")
        else:
            # Plain MARKET order fallback
            result = dhan_client.place_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="SCALP",
            )
            plain_order_id = str((result or {}).get("orderId") or (result or {}).get("id") or "") or None
    except Exception as e:
        error_msg = str(e)
        # AUDIT FIX (2026-09-15): classify the BUY-side rejection too — if
        # Dhan rejects the entry itself as "not allowed to be traded in
        # Intraday", record the restriction immediately so this symbol is
        # filtered out of future cycles before capital is ever reserved for
        # it. Previously, a BUY rejection just fell into the generic
        # ORDER_FAILED path with no diagnosis and no learning.
        if dhan_client.is_security_intraday_restricted_error(error_msg):
            logger.error(
                "position-stocks entry: BUY rejected — %s is INTRADAY_RESTRICTED "
                "(T2T/ASM/GSM). Recording restriction to exclude from future cycles. "
                "Error: %s", candidate.symbol, error_msg,
            )
            try:
                intraday_eligibility.record_restriction(
                    db, candidate.symbol,
                    detail=f"BUY rejection: {error_msg[:200]}",
                )
            except Exception as rec_e:
                logger.warning(
                    "position-stocks entry: could not record restriction for %s: %s",
                    candidate.symbol, rec_e,
                )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INTRADAY_RESTRICTED:{error_msg}", quality=quality)
        elif dhan_client.is_intraday_cutoff_error(error_msg):
            logger.warning(
                "position-stocks entry: BUY rejected — INTRADAY_CUTOFF: exchange window "
                "closed for today. Will retry tomorrow. Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INTRADAY_CUTOFF:{error_msg}", quality=quality)
        elif dhan_client.is_insufficient_funds_error(error_msg):
            logger.error(
                "position-stocks entry: BUY rejected — INSUFFICIENT_FUNDS. "
                "Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            entry_pause.pause_all("INSUFFICIENT_FUNDS", config.ENTRY_MARGIN_PAUSE_MINUTES)   # group209
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_INSUFFICIENT_FUNDS:{error_msg}", quality=quality)
        elif dhan_client.is_circuit_limit_error(error_msg):
            # 2026-09-15 fix (session41b): "Rate Not Within Ckt Limit X To Y"
            # — the stock has already hit its upper circuit; our BUY price
            # was outside the allowed band.  Retrying at the same price can
            # NEVER succeed this session.  Record it as intraday-restricted
            # (same mechanism as T2T/ASM stocks) so it's excluded from all
            # future cycles today — a stock at upper circuit has no intraday
            # exit room even if the BUY did fill.
            logger.error(
                "position-stocks entry: BUY rejected — CIRCUIT_LIMIT (stock at upper "
                "circuit, no intraday exit room). Recording restriction for today. "
                "Error: %s", error_msg,
            )
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            try:
                intraday_eligibility.record_restriction(db, candidate.symbol)
                logger.info(
                    "position-stocks entry: %s added to intraday-restricted set (circuit limit)",
                    candidate.symbol,
                )
            except Exception as rec_e:
                logger.warning(
                    "position-stocks entry: failed to record circuit-limit restriction for %s: %s",
                    candidate.symbol, rec_e,
                )
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED_CIRCUIT_LIMIT:{error_msg}", quality=quality)
        else:
            logger.error("position-stocks entry: order placement failed for %s: %s", candidate.symbol, e)
            ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
            entry_pause.cooldown_symbol(candidate.symbol, config.ENTRY_ORDER_FAILED_COOLDOWN_MINUTES, error_msg)   # group209
            _log_candidate(db, candidate, "SKIPPED", f"ORDER_FAILED:{error_msg}", quality=quality)
        # AUDIT FIX (session60): every branch above releases capital on a
        # failed BUY — the symbol claim taken above (before adaptive
        # levels were even computed) must be released here too, or a
        # failed order permanently locks this symbol out for this service
        # (real-trade-service is unaffected either way, since it never
        # held the lock).
        shared_symbol_lock.release(db, candidate.symbol)
        return None

    # Mark first-live-order done
    if is_first:
        gate.first_live_order_done = True

    # Order budget increment
    if gate.orders_placed_today_date != today:
        gate.orders_placed_today = 0
        gate.orders_placed_today_date = today
    gate.orders_placed_today += 1
    db.commit()

    # DB record
    pos = ScalpPosition(
        symbol=candidate.symbol,
        dhan_security_id=security_id,
        window_source=candidate.window_label,
        status="OPEN",
        entry_price=candidate.current_ltp,
        signal_price=candidate.current_ltp,                  # group280: slippage = fill vs this
        quantity=quantity,
        target_price=levels.target_price,
        stop_price=levels.stop_price,
        adaptive_target_pct=levels.target_pct,
        adaptive_stop_pct=levels.stop_pct,
        # AUDIT FIX (this session — "breakeven stop is dead code"):
        # orders/adaptive.py::compute() has always computed
        # breakeven_trigger_pct but nothing ever saved it — see models.py's
        # comment on this column. Persisted here so
        # orders/breakeven.py::run_breakeven_stop() has something to check
        # unrealized gain against for this position.
        breakeven_trigger_pct=levels.breakeven_trigger_pct,
        dhan_super_order_id=dhan_super_order_id,
        # AUDIT FIX (session22 cont'd): dhan_entry_order_id was declared on
        # the model but never written anywhere, so it was always NULL even
        # though the frontend/API now expose it. Per Dhan's super-order
        # response shape (see reconcile.py's docstring), the top-level
        # orderId of a super order IS the ENTRY_LEG's order id — there is
        # no separate id Dhan issues for it — so it's set equal to
        # dhan_super_order_id here rather than left to default to NULL.
        dhan_entry_order_id=dhan_super_order_id or plain_order_id,
        capital_risked=position_value,
        is_first_live_order=is_first,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)

    _nifty = trade_gates.last_nifty_change_pct()
    _log_candidate(db, candidate, "ENTERED",
                   f"SUPER_ORDER={dhan_super_order_id or 'plain_order'}"
                   f" nifty_pct={'n/a' if _nifty is None else f'{_nifty:+.2f}'}"
                   f" stop_pct={levels.stop_pct:.2f} target_pct={levels.target_pct:.2f}"
                   f"{opening_gate.entry_features(candidate.symbol, candidate.current_ltp)}",
                   composite_score=candidate.composite_score,
                   quality=quality)
    logger.info(
        "position-stocks ENTERED %s x%d @ ₹%.2f target=₹%.2f stop=₹%.2f "
        "(window=%s score=%.3f super_order=%s)",
        candidate.symbol, quantity, candidate.current_ltp,
        levels.target_price, levels.stop_price,
        candidate.window_label, candidate.composite_score,
        dhan_super_order_id or "N/A",
    )
    # BUG FIX (this session — "no notification got for position stocks
    # order on telegram"): notifier.py has existed since session41 but was
    # ONLY ever called for CRITICAL failures (broker mismatches, dead EOD
    # sells) — a completely normal, successful BUY never notified anyone,
    # unlike real-trade-service's entry_engine.py which notifies on every
    # BUY it sends. Mirrors that message shape (see entry_engine.py's
    # "📤 BUY sent (auto)" notify_async call), best-effort/non-blocking
    # same as every other notifier.py call in this codebase.
    notifier.notify_fire_and_forget(
        f"📤 <b>BUY placed</b> — {candidate.symbol} x{quantity}\n"
        f"Entry ₹{candidate.current_ltp:.2f} | Target ₹{levels.target_price:.2f} "
        f"({levels.target_pct:.2f}%) | Stop ₹{levels.stop_price:.2f} ({levels.stop_pct:.2f}%)\n"
        f"Window {candidate.window_label} | Capital ₹{position_value:,.2f} "
        f"| Super Order {dhan_super_order_id or 'N/A'}"
    )
    return pos


class InsufficientCapitalSkip(Exception):
    """Raised by attempt_entry() when the skip reason is purely a
    capital-sizing problem (INSUFFICIENT_CAPITAL or
    INSUFFICIENT_CAPITAL_FOR_MIN_QTY) — i.e. the candidate itself is
    fine but the pool cannot currently afford it.

    main.py's _run_cycle() catches this and continues to the next
    quality-passing candidate in rank order instead of ending the cycle,
    fixing the TREL/16x pattern where a cheaper already-vetted candidate
    sat unconsidered every cycle while the pool kept re-trying the one
    expensive stock it could not afford.

    Distinguished from a plain None return (other skip reasons such as
    MAX_POSITIONS, KILL_SWITCH, SECURITY_NOT_FOUND, REENTRY_GUARD) which
    correctly end the cycle — those are not capital-sizing problems and
    retrying the next candidate would not help.
    FIX: session70 audit."""


class ManualEntryRejected(Exception):
    """Raised by attempt_manual_entry() for any rejection the caller (a
    live admin request, not a background scan) should see as an explicit
    error response rather than a silently-logged skip — unlike
    attempt_entry() above (called from the unattended scan loop, where a
    None return + a ScalpCandidateLog row is the right contract)."""


def attempt_manual_entry(
    db: Session,
    symbol: str,
    current_ltp: float,
    quantity: Optional[int] = None,
) -> ScalpPosition:
    """Manual BUY — an admin picks the symbol (and optionally the exact
    quantity) directly from the dashboard, bypassing the screener/quality
    gate entirely (this IS the "manual control to buy" feature — the
    screener only ever proposes candidates automatically; there was no
    path for an admin to just buy something they're watching).

    Deliberately reuses every safety gate attempt_entry() enforces for an
    automatic candidate — armed check, daily-loss kill switch, per-day
    order budget, max-concurrent-positions cap, adaptive target/stop
    sizing, the shared cross-service Dhan order-rate guard, and the same
    Super Order (or plain MARKET fallback) placement path — so a manual
    BUY is exactly as safe as an automatic one, just skipping the
    window-scan/quality-gate SELECTION step, not any of the RISK checks.

    quantity: if given, sizes the position to exactly this many shares
    (reserving quantity * current_ltp from the ledger) instead of the
    automatic risk-based sizing attempt_entry() uses. Still subject to the
    same available_capital check — an admin can't manually buy more than
    the ledger has free, same as the automatic path can't.

    Raises ManualEntryRejected with a human-readable reason on any
    rejection (armed/kill-switch/budget/capital/security-id/Dhan) instead
    of returning None — this is a live admin action expecting an explicit
    error, not a background scan skip. Returns the created ScalpPosition
    on success.
    """
    gate = _get_gate_state(db)

    if not gate.is_armed:
        raise ManualEntryRejected("Service is not armed — arm it before placing a manual BUY.")

    if gate.daily_loss_kill_switch_tripped:
        raise ManualEntryRejected("Daily loss kill switch is tripped — no new entries today.")

    today = ist_today_str()
    if gate.orders_placed_today_date == today and gate.orders_placed_today >= config.DAILY_ORDER_BUDGET:
        raise ManualEntryRejected(f"Daily order budget exhausted ({gate.orders_placed_today}).")

    open_count = _count_open_positions(db)
    if open_count >= config.MAX_CONCURRENT_SCALP_POSITIONS:
        raise ManualEntryRejected(
            f"Max concurrent positions reached ({open_count}/{config.MAX_CONCURRENT_SCALP_POSITIONS})."
        )

    if current_ltp <= 0:
        raise ManualEntryRejected(f"No valid live price for {symbol}.")

    symbol = symbol.strip().upper()

    # group279: refuse a price from a feed that has gone quiet. Checked before the symbol claim so nothing has to be
    # released. The automatic path has had this since 2026-10-05 (_price_guard_reject); the manual route did not.
    _stale = _stale_tick_reject(symbol)
    if _stale:
        raise ManualEntryRejected(f"{symbol}: {_stale}. Wait for a fresh tick (is the market open?) and try again.")

    # AUDIT FIX (2026-09-18): shared_symbol_lock.try_claim() below returns
    # True whenever the lock is already held by THIS service (an
    # already-ours claim is treated as "not a conflict" — see its own
    # docstring), so it never blocked a second manual BUY of a symbol this
    # service already had an OPEN position in. This service has no
    # pyramiding/averaging-in concept (unlike real-trade-service, which
    # explicitly supports it) — MAX_CONCURRENT_SCALP_POSITIONS also counts
    # positions, not distinct symbols, so two rows for the same symbol
    # silently ate two slots of that cap instead of one. The automatic
    # screener already excludes any symbol with an open position
    # (screening/engine.py filters candidates against open ScalpPosition
    # rows); this closes the same gap for the manual path.
    existing_position = (
        db.query(ScalpPosition)
        .filter(ScalpPosition.symbol == symbol)
        .filter(ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")))
        .first()
    )
    if existing_position is not None:
        raise ManualEntryRejected(
            f"{symbol} already has an open position here (id={existing_position.id}, "
            f"status={existing_position.status}) — this service doesn't support "
            "averaging in. Close the existing position first if you want to re-enter."
        )

    # AUDIT FIX (session60): same cross-service symbol lock as attempt_entry()
    # — a manual BUY is exactly as capable of colliding with a
    # real-trade-service holding as an automatic one, and this is the
    # admin-facing path, so it gets an explicit rejection rather than a
    # silent skip.
    if not shared_symbol_lock.try_claim(db, symbol):
        raise ManualEntryRejected(
            f"{symbol} is already held by real-trade-service on the shared Dhan "
            "account — buying it here too would create a duplicate broker "
            "position. Close it on the other service first if you want to "
            "re-enter it here."
        )

    # Adaptive levels — pct_change=0.0 since this is a manual pick, not a
    # window-scan signal; compute() falls back to ATR-proxy from the tick
    # buffer when available (the normal case for any actively-traded NSE
    # symbol this service's WS feed has already ticked), and only falls
    # back further to a pct_change-derived stop when the buffer is too
    # thin — see adaptive.py's compute() docstring.
    levels: AdaptiveLevels = compute_levels(0.0, current_ltp, symbol=symbol)

    if quantity is not None:
        if quantity <= 0:
            shared_symbol_lock.release(db, symbol)
            raise ManualEntryRejected("quantity must be a positive integer.")
        exact_cost = quantity * current_ltp
        if not ledger.reserve_additional(db, exact_cost):
            shared_symbol_lock.release(db, symbol)
            raise ManualEntryRejected(
                f"Insufficient capital: need ₹{exact_cost:,.2f} for {quantity} shares of {symbol}."
            )
        position_value = exact_cost
    else:
        position_value = ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct)
        if position_value is None:
            shared_symbol_lock.release(db, symbol)
            raise ManualEntryRejected("Insufficient available capital (or daily-loss kill switch tripped).")
        quantity = max(1, int(position_value / current_ltp))
        actual_cost = quantity * current_ltp
        if actual_cost > position_value:
            shortfall = actual_cost - position_value
            if not ledger.reserve_additional(db, shortfall):
                ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
                shared_symbol_lock.release(db, symbol)
                raise ManualEntryRejected(
                    f"Insufficient capital to cover the minimum 1-share order "
                    f"(shortfall ₹{shortfall:,.2f})."
                )
            position_value = actual_cost

    try:
        security_id = dhan_client.get_security_id(db, symbol)
    except dhan_client.SecurityNotResolvedError as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        raise ManualEntryRejected(f"Could not resolve a Dhan security id for {symbol}: {e}")
    except Exception:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        raise

    if not shared_order_budget.check_and_reserve(db):
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        raise ManualEntryRejected("Shared cross-service Dhan order-rate budget exhausted for today.")

    is_first = not gate.first_live_order_done
    dhan_super_order_id = None
    plain_order_id = None
    try:
        if config.USE_SUPER_ORDER:
            result = dhan_client.place_super_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=current_ltp,
                target_price=levels.target_price,
                stop_loss_price=levels.stop_price,
                trailing_jump=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="MANUAL",
            )
            dhan_super_order_id = str(result.get("orderId") or result.get("id") or "")
        else:
            result = dhan_client.place_order(
                db,
                is_armed=gate.is_armed,
                security_id=security_id,
                exchange_segment=config.SCALP_EXCHANGE_SEGMENT,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                price=0.0,
                product_type=config.SCALP_PRODUCT_TYPE,
                tag="MANUAL",
            )
            plain_order_id = str((result or {}).get("orderId") or (result or {}).get("id") or "") or None
    except Exception as e:
        ledger.release_capital(db, position_value=position_value, realized_pnl=0.0)
        shared_symbol_lock.release(db, symbol)
        db.add(ScalpCandidateLog(
            symbol=symbol, window_source="MANUAL", pct_change=0.0,
            composite_score=None, decision="SKIPPED",
            reason=f"MANUAL_ORDER_FAILED:{e}",
        ))
        db.commit()
        raise ManualEntryRejected(f"Dhan rejected the manual BUY: {e}")

    if is_first:
        gate.first_live_order_done = True
    if gate.orders_placed_today_date != today:
        gate.orders_placed_today = 0
        gate.orders_placed_today_date = today
    gate.orders_placed_today += 1
    db.commit()

    pos = ScalpPosition(
        symbol=symbol,
        dhan_security_id=security_id,
        window_source="MANUAL",
        status="OPEN",
        entry_price=current_ltp,
        signal_price=current_ltp,                            # group280
        quantity=quantity,
        target_price=levels.target_price,
        stop_price=levels.stop_price,
        adaptive_target_pct=levels.target_pct,
        adaptive_stop_pct=levels.stop_pct,
        # this session: same fix as attempt_entry() above — see that
        # ScalpPosition() call's comment.
        breakeven_trigger_pct=levels.breakeven_trigger_pct,
        dhan_super_order_id=dhan_super_order_id,
        dhan_entry_order_id=dhan_super_order_id or plain_order_id,
        capital_risked=position_value,
        is_first_live_order=is_first,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)

    db.add(ScalpCandidateLog(
        symbol=symbol, window_source="MANUAL", pct_change=0.0,
        composite_score=None, decision="ENTERED",
        reason=f"MANUAL_BUY:SUPER_ORDER={dhan_super_order_id or 'plain_order'}",
    ))
    db.commit()

    logger.info(
        "position-stocks MANUAL BUY %s x%d @ ₹%.2f target=₹%.2f stop=₹%.2f super_order=%s",
        symbol, quantity, current_ltp, levels.target_price, levels.stop_price,
        dhan_super_order_id or "N/A",
    )
    notifier.notify_fire_and_forget(
        f"📤 <b>Manual BUY placed</b> — {symbol} x{quantity}\n"
        f"Entry ₹{current_ltp:.2f} | Target ₹{levels.target_price:.2f} "
        f"({levels.target_pct:.2f}%) | Stop ₹{levels.stop_price:.2f} ({levels.stop_pct:.2f}%)\n"
        f"Capital ₹{position_value:,.2f} | Super Order {dhan_super_order_id or 'N/A'}"
    )
    return pos