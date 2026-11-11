"""orders/claim_store.py - E366: durable copy of plain_short's in-memory queues (failed capital / symbol-lock releases, watched unknown-state SELLs), so a restart does not forget them.
Every function is best effort and NEVER raises: the in-memory queue stays the source of truth inside a process, this table only lets the next process pick the items up."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("position-stocks-claim-store")


def _rollback(db) -> None:
    try:
        db.rollback()
    except Exception as e:  # noqa: BLE001
        logger.warning("claim store: rollback failed (%s)", e)


def save(db, key: str, kind: str, payload: Dict[str, Any]) -> bool:
    """Insert or update one row. False when it could not be written."""
    if db is None or not key:
        return False
    try:
        from models import ScalpPendingClaim
        body = json.dumps(payload, separators=(",", ":"), default=str)[:4000]
        row = db.query(ScalpPendingClaim).filter(ScalpPendingClaim.key == str(key)).first()
        if row is None:
            db.add(ScalpPendingClaim(key=str(key)[:64], kind=str(kind)[:16], payload_json=body))
        else:
            row.kind, row.payload_json = str(kind)[:16], body
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        _rollback(db)
        logger.warning("claim store: could not save %s %s (%s)", kind, key, e)
        return False


def forget(db, key: str) -> bool:
    """Delete one row (the item resolved or was given up). False when it could not be deleted."""
    if db is None or not key:
        return False
    try:
        from models import ScalpPendingClaim
        db.query(ScalpPendingClaim).filter(ScalpPendingClaim.key == str(key)).delete()
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        _rollback(db)
        logger.warning("claim store: could not delete %s (%s)", key, e)
        return False


def load_all(db) -> Tuple[List[Tuple[str, str, Dict[str, Any]]], bool]:
    """([(key, kind, payload)], readable). A row whose payload cannot be parsed is skipped."""
    if db is None:
        return [], False
    try:
        from models import ScalpPendingClaim
        rows = db.query(ScalpPendingClaim).all()
        out = []
        for r in rows:
            try:
                out.append((str(r.key), str(r.kind), json.loads(r.payload_json)))
            except Exception as e:  # noqa: BLE001
                logger.warning("claim store: unreadable row %s skipped (%s)", getattr(r, "key", "?"), e)
        return out, True
    except Exception as e:  # noqa: BLE001
        _rollback(db)
        logger.warning("claim store: could not read the table (%s)", e)
        return [], False
