"""
execution/equity_sync.py — keeps trade_accounts.REAL in sync with the
user's actual Dhan balance.

ROOT-CAUSE FIX (2026-08-27): trade_accounts.REAL is seeded with
starting_capital=0.0 / cash_available=0.0 / current_equity=0.0 (see
main.py's _seed_defaults — REAL deliberately never gets a fake seed
capital the way DEMO gets DEFAULT_DEMO_CAPITAL, because REAL capital is
real money sitting in Dhan, not a number this service should invent).

Nothing else ever set REAL's equity after that. That means every
risk_engine check #4 (per_trade_risk_cap) saw
max_trade_risk = equity(0) * risk_per_trade_pct = 0, so it rejected
EVERY BUY — the same rejection whether the order came from a manual
ticket (Review comes back not-ok, so ManualTradeTicket.tsx never renders
the CONFIRM BUY button — this is why it looked like "there's no buy
button") or from the automatic entry_engine cycle. A small/limited
balance was never the actual blocker; a balance of literally zero (in
this service's own bookkeeping, not in Dhan) was.

Fix: before any risk decision or cycle for REAL, pull the live available
balance from Dhan (`get_fund_limits`) and refresh cash_available /
current_equity from it. This is intentionally NOT a one-time "set your
starting capital" admin field — a broker balance changes (deposits,
withdrawals, other apps trading the same account), so it's re-synced on
every read rather than trusted from a stale snapshot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from execution import dhan_client
from portfolio.portfolio import _open_positions_market_value, get_account

logger = logging.getLogger("real-trade-equity-sync")

# Dhan's SDK/API has returned the available-balance field under several
# names across versions — "availabelBalance" is Dhan's OWN historical typo
# in the live API, not a typo introduced here. Try every known spelling
# rather than trusting one; RealAutoTrade.tsx's pickNum() does the same
# thing on the frontend for the same reason.
#
# 2026-09-01 note: these five keys are NOT all the same underlying quantity
# — "sodLimit" is a start-of-day figure that doesn't reflect intraday
# usage, and "withdrawableBalance" can legitimately sit below the actual
# tradeable balance (funds blocked as margin/collateral aren't withdrawable
# but may still be usable for a new trade). The order below is a best-
# effort ranking (most-likely-current-and-tradeable first, start-of-day
# last), not a verified-against-live-Dhan-responses guarantee — this
# service has not had a live account exercise every one of these fields
# to confirm which actually populates on a real response. _pick_balance()
# now returns which key matched, and sync_real_equity() logs a WARNING the
# first time it's used and again any time the matched key CHANGES between
# syncs (e.g. Dhan stops populating "availableCash" and this silently
# starts falling through to "sodLimit" instead) — that's the actual signal
# worth watching for in production, since a silent fallback-key change
# would otherwise size every future trade off a different, unnoticed
# definition of "available equity" without any visible warning.
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


def sync_real_equity(db: Session) -> Optional[float]:
    """Best-effort refresh of trade_accounts.REAL from Dhan's live fund
    limits. Returns the new current_equity, or None if the sync could not
    happen (not connected / Dhan call failed / no recognizable balance
    field) — callers must keep working off whatever equity value is
    already in the DB in that case. A transient Dhan API hiccup must
    never crash or block the risk/entry path; it should just mean "sized
    off slightly stale balance this one cycle", never a 500."""
    try:
        funds = dhan_client.get_funds(db)
    except dhan_client.DhanNotConnectedError:
        return None
    except Exception as e:
        logger.warning("equity_sync: Dhan get_funds failed, keeping last-known equity: %s", e)
        return None

    balance, matched_key = _pick_balance(funds)
    if balance is None:
        logger.warning("equity_sync: Dhan funds response had no recognizable balance field: %s", funds)
        return None

    global _last_balance_key
    if matched_key != _last_balance_key:
        if _last_balance_key is None:
            logger.warning(
                "equity_sync: using Dhan balance field '%s' for REAL equity — "
                "verify this reflects a sensible current tradeable balance.",
                matched_key,
            )
        else:
            logger.warning(
                "equity_sync: Dhan balance field in use changed '%s' -> '%s' — "
                "the funds response shape shifted; verify the new field still "
                "reflects a sensible tradeable balance, not a stale/different-"
                "scoped figure.",
                _last_balance_key, matched_key,
            )
        _last_balance_key = matched_key

    account = get_account(db, "REAL")
    market_value = _open_positions_market_value(db, "REAL")
    account.cash_available = round(balance, 2)
    account.current_equity = round(balance + market_value, 2)
    if not account.starting_capital:
        # First successful sync only — gives max_daily_loss_pct a stable
        # reference point. Never overwritten again here, so a same-day
        # deposit/withdrawal doesn't silently reset the daily-loss
        # baseline mid-session.
        account.starting_capital = account.current_equity
    account.updated_at = datetime.now(timezone.utc)
    db.commit()
    return account.current_equity
