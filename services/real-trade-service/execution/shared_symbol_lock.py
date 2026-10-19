"""
execution/shared_symbol_lock.py — cross-service claim preventing this
service and position-stocks-service from both holding a live broker
position in the same symbol at once.

Duplicated from position-stocks-service/capital/shared_symbol_lock.py on
purpose (same isolation rationale as execution/shared_order_budget.py and
every other concept these two services share but don't share code for).

WHY: both services trade through the SAME Dhan account. Dhan holds one
consolidated position per symbol at the broker — it has no concept of
"these shares belong to real-trade-service" vs "these belong to
position-stocks-service". Confirmed in production: AEGISVOPAK was bought
by BOTH services on 17 Sept, each with its own local qty/entry/stop/target
row pointing at a share of one real, merged Dhan position. When either
side later sold on its own target/stop hit, its SELL was sized/priced
from only its own local record — Dhan's order book (reporting the true
merged position) didn't match what this service believed it had just
sent, which is exactly the "Broker order-type mismatch... investigate"
Telegram alert this was diagnosed from.

REAL-only: DEMO mode never touches the real broker, so it never claims or
releases this lock — only mode=="REAL" call sites check it (same
convention as shared_order_budget.check_and_reserve, which manual_engine
and entry.py only ever call on the REAL branch).

FAIL-CLOSED FOR ENTRIES (group 448; was fail-open): if the DB cannot answer,
try_claim() returns False, so the new BUY is refused and retried later, with a
CRITICAL log and a rate-limited Telegram. Only BUY entries are gated (exits
never are), so no position can get stuck. SHARED_GATES_FAIL_CLOSED=false
restores the old fail-open behavior.

Wired into:
  - entry_engine/entry.py's automatic REAL BUY path — claimed right before
    the Dhan LIMIT order is placed; released if that placement is
    REJECTED or if the order later EXPIRES unfilled (expire_stale_orders)
  - manual_engine.py's manual REAL BUY path — same claim/release shape
  - exit_engine/exit.py — released once a position's qty_open reaches 0
    (fully flat), not on a partial exit
  - portfolio.import_broker_holdings() (session62) — claimed before a
    pre-existing Dhan demat holding starts being actively managed by this
    service (stop/target evaluation, possible auto-SELL); the import is
    skipped for that symbol (not overridden) if position-stocks-service
    already holds the claim
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SharedSymbolLock

logger = logging.getLogger("real-trade-shared-symbol-lock")
SVC = "real-trade-service"

# ── Fail-closed on a gate error (group 448) ──────────────────────────────────
# Before: any DB error in this gate meant "allow the order", so during a DB hiccup both services could buy the same symbol / overshoot the shared
# daily order budget. These gates only ever guard new BUY entries (exits are never gated), so refusing an entry while the gate cannot answer is safe:
# the entry is simply skipped and retried later. Set SHARED_GATES_FAIL_CLOSED=false to get the old fail-open behaviour back.
_ALERT_EVERY_S = 600.0
_last_alert = {"t": None}


def _fail_closed() -> bool:
    try:
        import config
        return bool(getattr(config, "SHARED_GATES_FAIL_CLOSED", True))
    except Exception:  # noqa: BLE001
        return True


def _gate_failed(gate: str, err) -> bool:
    """What the gate returns when it could not decide: True (allow) only if fail-closed is switched off. Loud: CRITICAL log + a rate-limited Telegram."""
    if not _fail_closed():
        logger.error("%s: %s could not decide (%s) - failing OPEN, order allowed (SHARED_GATES_FAIL_CLOSED=false)", SVC, gate, err)
        return True
    logger.critical("%s: %s could not decide (%s) - failing CLOSED, this entry is refused", SVC, gate, err)
    import time as _time
    now = _time.monotonic()
    if _last_alert["t"] is None or now - _last_alert["t"] >= _ALERT_EVERY_S:
        _last_alert["t"] = now
        try:
            from notifier import notify_fire_and_forget
            notify_fire_and_forget(f"{SVC}: the {gate} check could not reach the database ({type(err).__name__}). "
                                   "New BUY entries are being refused until it recovers. Exits are not affected.")
        except Exception:  # noqa: BLE001 - the alert must never break the gate
            pass
    return False


_SERVICE_NAME = "real-trade-service"


def try_claim(db: Session, symbol: str, mode: str = "REAL") -> bool:
    """Call BEFORE a REAL BUY for `symbol` goes to Dhan. Returns True if
    the claim succeeded (no other service currently holds this symbol, or
    this service already holds it itself — e.g. an averaging-in add),
    False if position-stocks-service already holds it and the BUY should
    be rejected/skipped. On ANY error, logs and returns True (fail open)."""
    symbol = symbol.strip().upper()
    try:
        existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if existing is not None:
            if existing.held_by_service == _SERVICE_NAME:
                return True  # already ours — not a conflict
            logger.warning(
                "symbol lock BLOCKED buy of %s — already held by %s (mode=%s) since %s",
                symbol, existing.held_by_service, existing.held_by_mode, existing.claimed_at,
            )
            return False
        db.add(SharedSymbolLock(symbol=symbol, held_by_service=_SERVICE_NAME, held_by_mode=mode))
        db.commit()
        return True
    except IntegrityError:
        # Race: position-stocks-service (or a concurrent request here)
        # inserted the same symbol between our SELECT and our INSERT.
        db.rollback()
        try:
            existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
            if existing is not None and existing.held_by_service != _SERVICE_NAME:
                logger.warning(
                    "symbol lock BLOCKED buy of %s — lost race to %s",
                    symbol, existing.held_by_service,
                )
                return False
        except Exception as chk_e:  # noqa: BLE001
            logger.warning("symbol lock race re-check for %s failed (%s)", symbol, chk_e)
            return _gate_failed("symbol lock", chk_e)
        return True
    except Exception as e:
        logger.error("shared_symbol_lock.try_claim(%s) failed: %s", symbol, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return _gate_failed("symbol lock", e)


def release(db: Session, symbol: str) -> None:
    """Call once this service's position in `symbol` is fully flat
    (qty_open reaches 0 — see exit_engine/exit.py) or once a PLACED order
    for it is REJECTED/EXPIRED without ever filling. Only releases a row
    this service itself holds; never touches a row position-stocks-service
    holds. Never raises."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol, held_by_service=_SERVICE_NAME).first()
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception as e:
        logger.error("shared_symbol_lock.release(%s) failed (non-blocking): %s", symbol, e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


def status(db: Session) -> list[dict]:
    """Read-only snapshot for /status — every symbol currently locked by
    either service. Never raises."""
    try:
        rows = db.query(SharedSymbolLock).all()
        return [
            {
                "symbol": r.symbol,
                "held_by_service": r.held_by_service,
                "held_by_mode": r.held_by_mode,
                "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("shared_symbol_lock.status failed: %s", e, exc_info=True)
        return []
