"""
capital/shared_symbol_lock.py — cross-service claim preventing this service
and real-trade-service from both holding a live broker position in the
same symbol at once (session60 fix).

WHY: both services trade through the SAME Dhan account. Dhan holds one
consolidated position per symbol at the broker — it has no concept of
"these shares belong to position-stocks-service" vs "these belong to
real-trade-service". Confirmed in production: AEGISVOPAK was bought by
BOTH services on 17 Sept, each with its own local qty/entry/stop/target
row pointing at a share of one real, merged Dhan position. When either
side later sold on its own target/stop hit, its SELL was sized/priced
from only its own local record — Dhan's order book (reporting the true
merged position) didn't match what this service believed it had just
sent, which is exactly the "Broker order-type mismatch... investigate"
Telegram alert this was diagnosed from.

FAIL-CLOSED FOR ENTRIES (group 448; was fail-open): if the DB cannot answer,
try_claim() returns False, so the new BUY is skipped and retried later, with
a CRITICAL log and a rate-limited Telegram. Only BUY entries are gated (exits
never are), so no position can get stuck. SHARED_GATES_FAIL_CLOSED=false
restores the old fail-open behavior.

execution/shared_symbol_lock.py in real-trade-service is the duplicated
counterpart — same logic, not imported (same isolation rationale as
every other duplicated module shared between these two services)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SharedSymbolLock

logger = logging.getLogger("position-stocks-shared-symbol-lock")
SVC = "position-stocks-service"

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


def _err_brief(err, limit: int = 200) -> str:
    """First line of the driver's own message (e.g. 'ORA-00942: table or view does not exist'), so the Telegram alert says WHY, not only the class name."""
    try:
        raw = str(getattr(err, "orig", None) or err).strip()
        first = raw.splitlines()[0] if raw else ""
        return (first[:limit] or "no message")
    except Exception:  # noqa: BLE001
        return "no message"


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
            notify_fire_and_forget(f"{SVC}: the {gate} check could not reach the database ({type(err).__name__}: {_err_brief(err)}). "
                                   "New BUY entries are being refused until it recovers. Exits are not affected.")
        except Exception:  # noqa: BLE001 - the alert must never break the gate
            pass
    return False


_SERVICE_NAME = "position-stocks-service"


# ── E265: one transient DB error no longer refuses the entry ─────────────────
# Before, ANY exception in the claim (a DatabaseError from a slow / reconnecting Oracle, a pool timeout, a half-dead connection) went straight to
# fail-closed, so one blip refused the BUY and sent the "could not reach the database" alert. Now a database-driver error (sqlalchemy DBAPIError:
# DatabaseError / OperationalError / InterfaceError, a pool TimeoutError, PendingRollbackError) is retried SYMBOL_LOCK_DB_RETRIES times (default 1)
# after a rollback and a short pause; only when the retry fails too does the gate fail closed as before. A "table does not exist" error also
# re-creates the table (create with checkfirst, never ALTERs, at most once per 5 minutes). Programming errors (anything that is not a driver error)
# are NOT retried. SYMBOL_LOCK_DB_RETRIES=0 restores the exact old behaviour.
_ENSURE_TABLE_EVERY_S = 300.0
_last_ensure = {"t": None}


def _cfg_num(name: str, default, cast):
    try:
        import config
        return cast(getattr(config, name, default))
    except Exception:  # noqa: BLE001
        return default


def _is_transient_db_error(err) -> bool:
    """True for a database-driver / pool error that a second attempt can fix; False for anything else (a bug must not be retried)."""
    try:
        from sqlalchemy import exc as sa_exc
    except Exception:  # noqa: BLE001
        return False
    kinds = [sa_exc.DBAPIError]
    for name in ("TimeoutError", "PendingRollbackError"):
        k = getattr(sa_exc, name, None)
        if k is not None:
            kinds.append(k)
    return isinstance(err, tuple(kinds))


def _looks_like_missing_table(err) -> bool:
    text = f"{err} {getattr(err, 'orig', '')}".upper()
    return "ORA-00942" in text or "UNDEFINEDTABLE" in text or ("DOES NOT EXIST" in text and "RELATION" in text)


def _ensure_table(db: Session) -> None:
    """Create stockky_shared_symbol_lock when it is missing (checkfirst: an existing table is left alone). Rate-limited, never raises."""
    now = time.monotonic()
    if _last_ensure["t"] is not None and now - _last_ensure["t"] < _ENSURE_TABLE_EVERY_S:
        return
    _last_ensure["t"] = now
    try:
        SharedSymbolLock.__table__.create(bind=db.get_bind(), checkfirst=True)
        logger.warning("%s: symbol lock table was missing - created it (checkfirst)", SVC)
    except Exception as e:  # noqa: BLE001
        logger.error("%s: could not re-create the symbol lock table: %s", SVC, e)


def _claim_once(db: Session, symbol: str) -> bool:
    """One claim attempt. Returns True / False for a decision; a database error other than the unique-constraint race is RAISED to try_claim."""
    try:
        existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if existing is not None:
            if existing.held_by_service == _SERVICE_NAME:
                return True  # already ours (e.g. re-entry attempt) — not a conflict
            logger.warning(
                "position-stocks: symbol lock BLOCKED buy of %s — already held by %s (mode=%s) since %s",
                symbol, existing.held_by_service, existing.held_by_mode, existing.claimed_at,
            )
            return False
        db.add(SharedSymbolLock(symbol=symbol, held_by_service=_SERVICE_NAME, held_by_mode=None))
        db.commit()
        return True
    except IntegrityError:
        # Race: real-trade-service (or a concurrent request here) inserted
        # the same symbol between our SELECT and our INSERT. The unique
        # constraint on `symbol` caught it — treat exactly like the
        # "existing row found" branch above.
        db.rollback()
        try:
            existing = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
            if existing is not None and existing.held_by_service != _SERVICE_NAME:
                logger.warning(
                    "position-stocks: symbol lock BLOCKED buy of %s — lost race to %s",
                    symbol, existing.held_by_service,
                )
                return False
        except Exception as chk_e:  # noqa: BLE001
            logger.warning("position-stocks: symbol lock race re-check for %s failed (%s)", symbol, chk_e)
            return _gate_failed("symbol lock", chk_e)
        return True


def try_claim(db: Session, symbol: str) -> bool:
    """Call BEFORE a BUY for `symbol` goes to Dhan. Returns True if the
    claim succeeded (no other service currently holds this symbol, or
    this service already holds it itself — e.g. an averaging-in add),
    False if real-trade-service already holds it and the BUY should be
    skipped this cycle.

    On a database error the claim is retried (E265, see above); if it still cannot be decided, the gate fails CLOSED
    (group 448): False, CRITICAL log + a rate-limited Telegram naming the real error. SHARED_GATES_FAIL_CLOSED=false
    makes that True (fail open) instead."""
    symbol = symbol.strip().upper()
    retries = max(0, _cfg_num("SYMBOL_LOCK_DB_RETRIES", 1, int))
    delay = max(0.0, _cfg_num("SYMBOL_LOCK_DB_RETRY_DELAY_S", 0.3, float))
    attempt = 0
    while True:
        try:
            return _claim_once(db, symbol)
        except Exception as e:
            logger.error("position-stocks: shared_symbol_lock.try_claim(%s) failed (attempt %d): %s", symbol, attempt + 1, e, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
            if attempt < retries and _is_transient_db_error(e):
                if _looks_like_missing_table(e):
                    _ensure_table(db)
                attempt += 1
                logger.warning("position-stocks: symbol lock claim for %s hit a database error - retrying (%d/%d)", symbol, attempt, retries)
                if delay > 0:
                    time.sleep(delay * attempt)
                continue
            return _gate_failed("symbol lock", e)


def release(db: Session, symbol: str) -> None:
    """Call once this service's position in `symbol` is fully flat
    (closed/target hit/stop hit/EOD squareoff/manual exit — any terminal
    state). Only releases a row this service itself holds; never touches
    a row real-trade-service holds. Never raises — a release failure must
    not block the exit that triggered it; worst case the row is left
    stale and simply blocks this service's own re-entry into the same
    symbol until manually cleared, which is safe (if a little annoying)
    rather than dangerous."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol, held_by_service=_SERVICE_NAME).first()
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception as e:
        logger.error("position-stocks: shared_symbol_lock.release(%s) failed (non-blocking): %s", symbol, e, exc_info=True)
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
        logger.error("position-stocks: shared_symbol_lock.status failed: %s", e, exc_info=True)
        return []


def force_release(db: Session, symbol: str) -> bool:
    """Admin-only: unconditionally delete the lock row for `symbol`
    regardless of which service holds it. Used to clear stuck locks
    (e.g. TREL held_by_mode=null after a dead-entry error that didn't
    call release()). Returns True if a row was deleted, False if none
    existed. Never raises."""
    symbol = symbol.strip().upper()
    try:
        row = db.query(SharedSymbolLock).filter_by(symbol=symbol).first()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        logger.warning(
            "position-stocks: shared_symbol_lock.force_release(%s) — "
            "lock held by %s (mode=%s) cleared by admin",
            symbol, row.held_by_service, row.held_by_mode,
        )
        return True
    except Exception as e:
        logger.error(
            "position-stocks: shared_symbol_lock.force_release(%s) failed: %s",
            symbol, e, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _as_utc(dt):
    """claimed_at comes back naive from SQLite and aware from Postgres; compare both as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sweep(db: Session, min_age_s: float, keep_dead_sell: bool) -> list[str]:
    """Release locks held by THIS service that have no live position behind them.

    min_age_s: leave a claim younger than this alone. try_claim() runs BEFORE the ScalpPosition row
        exists, so a sweep with no grace period could delete the claim of an entry that is being placed
        right now. Startup passes 0 (nothing is in flight yet).
    keep_dead_sell: keep the lock of an ERROR row whose exit SELL died with zero fill (`*_SELL_DEAD`):
        the position may still be open at the broker and reconcile re-claimed the lock on purpose.
    """
    released: list[str] = []
    try:
        from models import ScalpPosition  # local to avoid circular import
        now = datetime.now(timezone.utc)
        our_locks = db.query(SharedSymbolLock).filter_by(held_by_service=_SERVICE_NAME).all()
        for lock in our_locks:
            if min_age_s > 0:
                claimed = _as_utc(lock.claimed_at)
                if claimed is not None and (now - claimed).total_seconds() < min_age_s:
                    continue
            has_open = db.query(ScalpPosition).filter(
                ScalpPosition.symbol == lock.symbol,
                ScalpPosition.status.in_(("OPEN", "EXIT_LEGS_REJECTED")),
            ).first()
            if has_open is not None:
                continue
            if keep_dead_sell:
                dead = db.query(ScalpPosition).filter(
                    ScalpPosition.symbol == lock.symbol,
                    ScalpPosition.status == "ERROR",
                    ScalpPosition.error_message.like("%_SELL_DEAD%"),
                ).first()
                if dead is not None:
                    continue
            db.delete(lock)
            released.append(lock.symbol)
            logger.warning(
                "position-stocks: shared_symbol_lock sweep: released stale lock for %s (no open position found)",
                lock.symbol,
            )
        if released:
            db.commit()
    except Exception as e:
        logger.error("position-stocks: shared_symbol_lock.cleanup_stale failed (sweep): %s", e, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return []
    return released


def cleanup_stale(db: Session) -> list[str]:
    """BUG FIX (Issue #4): on startup, sweep SharedSymbolLock rows held by THIS service and release any
    whose symbol has no corresponding OPEN or EXIT_LEGS_REJECTED ScalpPosition — meaning the position was
    closed/errored but release() was never called. Safe at startup because nothing is in flight yet.
    Returns the list of released symbols."""
    return _sweep(db, 0.0, False)


# group 272: "never swept" must be -inf, not 0.0: time.monotonic() is host uptime, so with 0.0 the first sweep was throttled
# whenever the host had been up for less than the interval (and a 3600 s interval made the test fail on a fresh VM).
_last_sweep = [float("-inf")]


def sweep_stale(db: Session, *, force: bool = False) -> list[str]:
    """Group 210 (item 14): the same sweep, safe to run while the service is trading.

    Before this the sweep ran only at startup, so a lock left behind by an ERROR row (AVALON) blocked
    that symbol until the next restart. Differences from cleanup_stale: claims younger than
    SYMBOL_LOCK_SWEEP_MIN_AGE_S (default 600) are left alone because their position row may not exist
    yet, and a lock behind a dead exit SELL is kept because that position may still be open. Throttled to
    one pass per SYMBOL_LOCK_SWEEP_INTERVAL_S (default 60; 0 turns the periodic sweep off).
    Never raises."""
    try:
        import config
        interval = float(getattr(config, "SYMBOL_LOCK_SWEEP_INTERVAL_S", 60.0))
        min_age = float(getattr(config, "SYMBOL_LOCK_SWEEP_MIN_AGE_S", 600.0))
    except Exception:
        interval, min_age = 60.0, 600.0
    if interval <= 0 and not force:
        return []
    now = time.monotonic()
    if not force and (now - _last_sweep[0]) < interval:
        return []
    _last_sweep[0] = now
    return _sweep(db, min_age, True)


def reset_sweep_throttle() -> None:
    """Tests: forget the last sweep time."""
    _last_sweep[0] = float("-inf")
