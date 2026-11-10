"""
E338 - the real capital ledger and the shared symbol lock (position-stocks-service).

1. capital/shared_symbol_lock._claim_once: when our INSERT collided (IntegrityError) and the re-read found NO row, the claim returned True
   although this service held nothing, so the BUY went out with no lock and real-trade-service could buy the same symbol. The INSERT is now
   tried once more; two collisions with still no row = fail closed (False), like every other gate error.
2. capital/ledger.py: a NaN / inf amount (a "nan" Dhan balance, a NaN P&L) made available_capital or realized_pnl_today NaN for good:
   `position_value > nan` is False (every size passes the capital check) and `nan >= limit` is False (the daily-loss kill switch never trips).
   Amounts are now finite or 0 (CRITICAL log), a ledger row that already holds NaN / inf is set back to 0, a non-finite Dhan balance field is skipped.

Run from services/position-stocks-service:   python3 -m pytest tests/test_e338_lock_ledger_nonfinite.py -q
"""
from __future__ import annotations

import math
import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import config
from sqlalchemy.exc import IntegrityError

from capital import ledger
from capital import shared_symbol_lock as sl
from tz_utils import ist_today_str

NAN, INF = float("nan"), float("inf")


# ── symbol lock ──────────────────────────────────────────────────────────────
def _collide():
    return IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def _quiet_alerts():
    sent = []
    sys.modules["notifier"] = types.SimpleNamespace(notify_fire_and_forget=lambda text: sent.append(text))
    sl._last_alert["t"] = None
    return sent


def _row(service):
    return types.SimpleNamespace(held_by_service=service, held_by_mode=None, claimed_at=None)


def test_collision_then_row_gone_then_second_insert_works_returns_true_with_a_row_held():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, None, None]    # check, re-read (gone), check again
    db.commit.side_effect = [_collide(), None]                                              # first INSERT collides, second works
    assert sl._claim_once(db, "ABC") is True
    assert db.add.call_count == 2 and db.commit.call_count == 2                              # a row really was inserted the second time
    db.rollback.assert_called_once()


def test_collision_with_no_row_twice_is_refused_not_granted():
    sent = _quiet_alerts()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, None, None, None]
    db.commit.side_effect = _collide()
    assert sl.try_claim(db, "ABC") is False
    assert db.rollback.call_count == 2
    assert len(sent) == 1 and "New BUY entries are being refused" in sent[0]


def test_collision_then_peer_row_is_still_a_lost_race():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, _row("real-trade-service")]
    db.commit.side_effect = _collide()
    assert sl._claim_once(db, "ABC") is False
    assert db.add.call_count == 1


def test_collision_then_our_own_row_is_still_allowed():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, _row("position-stocks-service")]
    db.commit.side_effect = _collide()
    assert sl._claim_once(db, "ABC") is True


def test_plain_claim_and_peer_held_are_unchanged():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    assert sl._claim_once(db, "ABC") is True
    assert db.add.call_count == 1 and db.commit.call_count == 1
    db2 = MagicMock()
    db2.query.return_value.filter_by.return_value.first.return_value = _row("real-trade-service")
    assert sl._claim_once(db2, "ABC") is False
    db2.add.assert_not_called()


def test_reread_failure_after_a_collision_still_fails_closed():
    _quiet_alerts()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.side_effect = [None, RuntimeError("reread failed")]
    db.commit.side_effect = _collide()
    assert sl.try_claim(db, "ABC") is False


# ── ledger ───────────────────────────────────────────────────────────────────
def _ledger_row(**kw):
    base = dict(total_allocated_capital=10000.0, available_capital=5000.0, realized_pnl_today=0.0, realized_pnl_total=0.0,
                daily_loss_kill_switch_tripped=False, daily_loss_kill_switch_tripped_date=None, pnl_last_reset_date=ist_today_str())
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Patched:
    """Point ledger._get_or_create at a plain row and silence publish_daily_pnl, then restore."""
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        self._g, self._p = ledger._get_or_create, ledger.publish_daily_pnl
        ledger._get_or_create = lambda db: self.row
        ledger.publish_daily_pnl = lambda db: None
        return self.row

    def __exit__(self, *a):
        ledger._get_or_create, ledger.publish_daily_pnl = self._g, self._p


def test_finite_amount_passes_numbers_and_zeroes_junk():
    assert ledger._finite_amount("x", 12.5) == 12.5
    assert ledger._finite_amount("x", "7") == 7.0
    assert ledger._finite_amount("x", -3) == -3.0
    for bad in (NAN, INF, -INF, "nan", "inf", None, "abc", object()):
        assert ledger._finite_amount("x", bad) == 0.0


def test_release_capital_normal_exit_is_unchanged():
    with _Patched(_ledger_row()) as row:
        ledger.release_capital(MagicMock(), position_value=1000.0, realized_pnl=-200.0)
    assert row.available_capital == 5800.0 and row.realized_pnl_today == -200.0 and row.realized_pnl_total == -200.0


def test_release_capital_nan_pnl_returns_the_capital_and_books_no_pnl():
    with _Patched(_ledger_row()) as row:
        ledger.release_capital(MagicMock(), position_value=1000.0, realized_pnl=NAN)
    assert row.available_capital == 6000.0
    assert row.realized_pnl_today == 0.0 and row.realized_pnl_total == 0.0
    assert all(math.isfinite(getattr(row, n)) for n in ("available_capital", "realized_pnl_today", "realized_pnl_total"))


def test_release_capital_nan_value_still_books_the_pnl_and_stays_finite():
    with _Patched(_ledger_row()) as row:
        ledger.release_capital(MagicMock(), position_value=NAN, realized_pnl=150.0)
    assert row.available_capital == 5150.0 and row.realized_pnl_today == 150.0


def test_kill_switch_still_trips_on_a_finite_loss_after_a_nan_was_refused():
    old = config.MAX_DAILY_LOSS_PCT_OF_POOL
    config.MAX_DAILY_LOSS_PCT_OF_POOL = 4.0
    try:
        with _Patched(_ledger_row()) as row:
            ledger.release_capital(MagicMock(), position_value=0.0, realized_pnl=NAN)
            assert row.daily_loss_kill_switch_tripped is False
            ledger.release_capital(MagicMock(), position_value=0.0, realized_pnl=-500.0)      # 5 % of a 10000 pool
            assert row.daily_loss_kill_switch_tripped is True
    finally:
        config.MAX_DAILY_LOSS_PCT_OF_POOL = old


def test_the_other_ledger_writers_ignore_a_non_finite_amount():
    with _Patched(_ledger_row()) as row:
        db = MagicMock()
        ledger.reconcile_position_cost(db, delta=NAN)
        ledger.reclaim_premature_release(db, capital_risked=INF)
        ledger.adjust_closed_pnl_today(db, delta=NAN)
        ledger.book_late_realized_pnl(db, NAN)
        assert ledger.reserve_additional(db, NAN) is False
        assert ledger.reserve_additional(db, INF) is False
    assert row.available_capital == 5000.0 and row.realized_pnl_today == 0.0 and row.realized_pnl_total == 0.0


def test_the_other_ledger_writers_still_work_on_finite_amounts():
    with _Patched(_ledger_row()) as row:
        db = MagicMock()
        ledger.reconcile_position_cost(db, delta=100.0)
        assert row.available_capital == 4900.0
        ledger.reclaim_premature_release(db, capital_risked=400.0)
        assert row.available_capital == 4500.0
        ledger.adjust_closed_pnl_today(db, delta=-50.0)
        assert row.available_capital == 4450.0 and row.realized_pnl_today == -50.0
        ledger.book_late_realized_pnl(db, 20.0)
        assert row.available_capital == 4470.0 and row.realized_pnl_total == -30.0 and row.realized_pnl_today == -50.0
        assert ledger.reserve_additional(db, 70.0) is True and row.available_capital == 4400.0
        assert ledger.reserve_additional(db, 99999.0) is False
        assert ledger.reserve_additional(db, 0) is True


def test_a_row_that_already_holds_nan_is_healed_when_read():
    row = _ledger_row(available_capital=NAN, realized_pnl_today=INF)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = row
    got = ledger._get_or_create(db)
    assert got is row
    assert row.available_capital == 0.0 and row.realized_pnl_today == 0.0
    assert row.total_allocated_capital == 10000.0 and row.realized_pnl_total == 0.0        # healthy fields untouched
    db.commit.assert_called()


def test_a_healthy_row_is_not_committed_or_changed_on_read():
    row = _ledger_row()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = row
    ledger._get_or_create(db)
    assert (row.total_allocated_capital, row.available_capital, row.realized_pnl_today) == (10000.0, 5000.0, 0.0)
    db.commit.assert_not_called()


def test_heal_ignores_non_numbers_and_never_raises():
    row = types.SimpleNamespace(total_allocated_capital=None, available_capital="x", realized_pnl_today=True, realized_pnl_total=MagicMock())
    db = MagicMock()
    ledger._heal_non_finite(db, row)
    assert row.total_allocated_capital is None and row.available_capital == "x" and row.realized_pnl_today is True
    db.commit.assert_not_called()
    bad_db = MagicMock()
    bad_db.commit.side_effect = RuntimeError("db down")
    ledger._heal_non_finite(bad_db, _ledger_row(available_capital=NAN))        # swallowed, rolled back
    bad_db.rollback.assert_called()


def test_pick_balance_skips_a_non_finite_field_and_uses_the_next():
    assert ledger._pick_balance({"availabelBalance": "nan", "availableBalance": 1234.5}) == (1234.5, "availableBalance")
    assert ledger._pick_balance({"availabelBalance": "inf", "availableBalance": "-inf", "availableCash": 99}) == (99.0, "availableCash")
    assert ledger._pick_balance({"availabelBalance": NAN}) == (None, None)
    assert ledger._pick_balance({"availabelBalance": 800}) == (800.0, "availabelBalance")
    assert ledger._pick_balance({"availabelBalance": "x", "availableBalance": 5}) == (5.0, "availableBalance")


def test_reserve_capital_refuses_a_sized_nan_instead_of_reserving_it():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capital", "ledger.py"), encoding="utf-8").read()
    i_check = src.index("if not math.isfinite(position_value):")
    i_gate = src.index("if position_value > row.available_capital:")
    i_take = src.index("row.available_capital -= position_value")
    assert i_check < i_gate < i_take
