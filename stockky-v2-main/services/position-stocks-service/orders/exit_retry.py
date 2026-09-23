"""
orders/exit_retry.py — per-position exit-PLACEMENT backoff for
position-stocks-service.

WHY THIS MODULE EXISTS (2026-09-20 audit finding)
real-trade-service already has this protection (models.py::
consecutive_exit_failures / exit_engine/exit.py::_should_skip_exit_this_cycle
/ execution/reconcile.py's reset-on-fill), added in that service's session40
after the DATAMATICS incident: 89 consecutive REJECTED zero-fill exit-SELL
attempts over ~4.5h, with no backoff and no operator alert, because nothing
remembered that the SAME position's exit had just failed a moment ago.

position-stocks-service never got the equivalent. Its fast loop calls
run_stagnation_exit() every cycle (~10-30s) against every OPEN position
meeting stagnation criteria, and orders/eod_squareoff.py's manual-exit path
is reachable on every admin click. If _fire_flat_sell()'s PLACEMENT itself
keeps failing (a persistent broker rejection — surveillance-restricted
security, margin shortfall — not merely a slow fill), the position never
leaves "OPEN"/its pre-exit status, so the very next cycle retries it again,
with zero memory of the prior failure. That is the identical unthrottled-
retry shape as the DATAMATICS incident, just at the placement step instead
of the fill-confirmation step.

This module closes that gap the same way real-trade-service did, scoped
to PLACEMENT (not fills): a SELL order that gets successfully ACCEPTED by
the broker but later dies with zero fill is already handled by
reconcile.py's dead-order path, which moves the position to ERROR / out of
the OPEN pool — that case is not retried by this loop and needs no cooldown
here. This module only throttles the case where placement itself never
succeeds.

Applies uniformly to every _fire_flat_sell() caller (EOD squareoff, manual
exit, stagnation exit) — same as real-trade-service's _send_real_sell()
gates both automatic and manual exits through one shared check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import config
import notifier
from models import ScalpPosition
from tz_utils import as_aware

logger = logging.getLogger("position-stocks-exit-retry")


class ExitInCooldown(Exception):
    """Raised by _fire_flat_sell when a prior placement-failure streak's
    backoff window hasn't elapsed yet. Callers should skip this position
    for the current cycle (same as any other non-fatal exit-skip reason),
    not treat it as a new failure."""


def cooldown_remaining_seconds(pos: ScalpPosition) -> float:
    """0.0 if not currently in cooldown (or has never failed); seconds
    remaining otherwise. Cooldown formula matches real-trade-service's
    exactly: BASE * 2^(failures-1), capped at MAX."""
    if not (pos.consecutive_exit_failures and pos.last_exit_failure_at):
        return 0.0
    cooldown = min(
        config.EXIT_RETRY_MAX_COOLDOWN_SECONDS,
        config.EXIT_RETRY_BASE_COOLDOWN_SECONDS * (2 ** (pos.consecutive_exit_failures - 1)),
    )
    elapsed = (datetime.now(timezone.utc) - as_aware(pos.last_exit_failure_at)).total_seconds()
    return max(0.0, cooldown - elapsed)


def check_cooldown(pos: ScalpPosition) -> None:
    """Raises ExitInCooldown if this position is still backing off from a
    prior placement-failure streak. Call at the very top of
    _fire_flat_sell, before any broker call is attempted."""
    remaining = cooldown_remaining_seconds(pos)
    if remaining > 0:
        raise ExitInCooldown(
            f"{pos.symbol} (id={pos.id}) — in exit-placement backoff after "
            f"{pos.consecutive_exit_failures} consecutive failure(s); "
            f"{remaining:.0f}s of cooldown remaining."
        )


def record_failure(db: Session, pos: ScalpPosition, reason: str) -> None:
    """Call when _fire_flat_sell exhausts every retry attempt without ever
    getting an order_id back — i.e. placement itself failed, not just a
    slow fill. Increments the streak and alerts once it crosses
    EXIT_RETRY_ALERT_THRESHOLD (and again on every further multiple, so a
    very long stuck streak doesn't go silent after the first alert)."""
    pos.consecutive_exit_failures = (pos.consecutive_exit_failures or 0) + 1
    pos.last_exit_failure_at = datetime.now(timezone.utc)
    db.commit()

    n = pos.consecutive_exit_failures
    threshold = config.EXIT_RETRY_ALERT_THRESHOLD
    if threshold > 0 and n >= threshold and n % threshold == 0:
        cooldown = min(
            config.EXIT_RETRY_MAX_COOLDOWN_SECONDS,
            config.EXIT_RETRY_BASE_COOLDOWN_SECONDS * (2 ** (n - 1)),
        )
        logger.critical(
            "exit-retry: %s (id=%d) — %d consecutive exit-placement failures "
            "(latest: %s). Backing off %.0fs before the next automatic attempt.",
            pos.symbol, pos.id, n, reason, cooldown,
        )
        try:
            notifier.notify_critical(
                f"🚨 <b>Exit SELL placement stuck</b> — {pos.symbol} (id={pos.id})\n"
                f"{n} consecutive placement failures with no order ever accepted by the "
                f"broker (latest: {reason[:200]}).\n"
                f"Backing off to a {cooldown:.0f}s cooldown before the next automatic retry. "
                f"This many identical failures in a row usually means something structural "
                f"(surveillance-restricted security, margin shortfall, or a genuinely "
                f"unsellable position right now) — please check manually rather than assume "
                f"it clears on its own."
            )
        except Exception as _ne:
            logger.warning("exit-retry: notify_critical failed for %s: %s", pos.symbol, _ne)
    else:
        logger.warning(
            "exit-retry: %s (id=%d) — exit-placement failure #%d (latest: %s).",
            pos.symbol, pos.id, n, reason,
        )


def reset(pos: ScalpPosition) -> None:
    """Call the moment a flat-SELL placement actually succeeds (an
    order_id was returned by the broker) — clears the streak so a
    position that finally places cleanly goes back to normal handling.
    Does not commit; every call site already commits pos shortly after."""
    if pos.consecutive_exit_failures or pos.last_exit_failure_at:
        pos.consecutive_exit_failures = 0
        pos.last_exit_failure_at = None
