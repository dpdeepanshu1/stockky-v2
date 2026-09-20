"""
tests/test_stuck_pending_status_gap.py — regression test for a bug found from
LIVE data (session 75, 2026-09-20): resolve_stuck_pending() required
status IN ("EOD_SQUAREOFF", "MANUAL_EXIT", "STAGNATION_EXIT") before it would
even look at a row, but list_pending_reconcile() (GET /reconcile/pending) has
never had that restriction — it matches on the error_message sentinel alone.
Two live rows (INDORAMA, NAHARINDUS) had status="STOP_HIT" with a leftover
"EOD_SQUAREOFF_PENDING_RECONCILE" sentinel and an already-correct exit_price/
realized_pnl (i.e. exactly the "self-heal" case the sweep's own docstring
describes) — but sat unresolved indefinitely because the status filter
silently excluded them from ever reaching that self-heal check.

This test recreates that exact shape and asserts resolve_stuck_pending()
self-heals it (clears error_message) regardless of status.

Run from services/position-stocks-service:
    python -m pytest tests/test_stuck_pending_status_gap.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import orders.reconcile as reconcile

_engine = create_engine("sqlite:///:memory:")


def _make_stuck_row(db, *, status: str, symbol="INDORAMA"):
    """Recreates the live shape: a real, already-resolved exit (exit_price !=
    entry_price, nonzero realized_pnl) but a leftover EOD_SQUAREOFF sentinel
    and a status the old filter didn't recognize."""
    pos = models.ScalpPosition(
        symbol=symbol,
        dhan_security_id="999",
        window_source="5m",
        status=status,
        entry_price=91.88,
        exit_price=90.04,
        quantity=23,
        realized_pnl=-42.32,
        realized_pnl_pct=-2.0,
        target_price=95.0,
        stop_price=90.0,
        adaptive_target_pct=5.0,
        adaptive_stop_pct=3.0,
        capital_risked=0.0,
        opened_at=datetime.now(timezone.utc) - timedelta(days=3),
        closed_at=datetime.now(timezone.utc) - timedelta(days=2),
        dhan_exit_order_id="34326091814575",
        error_message=(
            "EOD_SQUAREOFF_PENDING_RECONCILE: exit_price=entry_price "
            "placeholder until next reconcile pass fills in the real fill price."
        ),
    )
    db.add(pos)
    db.commit()
    return pos


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    session = sessionmaker(bind=_engine)()
    reconcile._last_stuck_sweep_ts = 0.0
    reconcile._stuck_alerted.clear()
    yield session
    session.close()


def test_stop_hit_row_is_self_healed_not_skipped(db):
    """The exact live bug: status=STOP_HIT (outside the old
    _FLAT_SELL_PENDING_STATUSES filter) with an already-correct exit_price/
    realized_pnl must still be picked up and self-healed."""
    pos = _make_stuck_row(db, status="STOP_HIT")

    summary = reconcile.resolve_stuck_pending(db, force=True)

    db.refresh(pos)
    assert summary["examined"] == 1
    assert summary["self_healed"] == 1
    assert pos.error_message is None


def test_target_hit_row_is_also_self_healed(db):
    """Same gap, different non-flat-sell status."""
    pos = _make_stuck_row(db, status="TARGET_HIT", symbol="NAHARINDUS")

    summary = reconcile.resolve_stuck_pending(db, force=True)

    db.refresh(pos)
    assert summary["self_healed"] == 1
    assert pos.error_message is None


def test_eod_squareoff_row_still_works_unchanged(db):
    """Sanity: the previously-covered statuses still work after removing the
    filter (nothing regressed for the cases that already passed)."""
    pos = _make_stuck_row(db, status="EOD_SQUAREOFF", symbol="TESTEOD")

    summary = reconcile.resolve_stuck_pending(db, force=True)

    db.refresh(pos)
    assert summary["self_healed"] == 1
    assert pos.error_message is None


def test_open_position_without_sentinel_is_never_touched(db):
    """Confirms dropping the status filter doesn't widen scope beyond rows
    that actually carry the sentinel — a normal OPEN position must be
    completely ignored by this sweep."""
    pos = models.ScalpPosition(
        symbol="STILLOPEN",
        dhan_security_id="1",
        window_source="5m",
        status="OPEN",
        entry_price=100.0,
        quantity=10,
        target_price=105.0,
        stop_price=97.0,
        adaptive_target_pct=5.0,
        adaptive_stop_pct=3.0,
        capital_risked=1000.0,
        opened_at=datetime.now(timezone.utc),
        error_message=None,
    )
    db.add(pos)
    db.commit()

    summary = reconcile.resolve_stuck_pending(db, force=True)

    db.refresh(pos)
    assert summary["examined"] == 0
    assert pos.status == "OPEN"
    assert pos.error_message is None
