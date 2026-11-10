"""
E324 - reconcile must never book a fill at an unusable price (0, NaN, inf, non-numeric).

Dhan's order book can carry averageTradedPrice = 0 on a row that reports filled shares (a present
field, so `_get` returns it and the old `fill_price is None` guard let it through). Booking it made a
BUY cost Rs 0, or a SELL a total loss that then tripped the daily-loss limit. An unusable price is now
handled like a missing one: the order stays open and the next cycle retries.

Reuses the real-portfolio / scripted-broker harness from test_rt_reconcile.py.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e324_reconcile_unusable_price.py -q
"""
from __future__ import annotations

import pytest

import models
from execution import reconcile as R
from test_rt_reconcile import (  # noqa: F401  (env is a pytest fixture)
    START_CASH, cash, env, mk_order, mk_pos, positions, row, run,
)

BAD_PRICES = [0.0, -5.0, float("nan"), float("inf"), "n/a", [100.0]]


def go(db):
    return run(R.reconcile_real_orders(db))


@pytest.mark.parametrize("v", [100.0, 0.01, "100.5", 1])
def test_usable_price_accepts_positive_finite(v):
    assert R._usable_price(v) is True


@pytest.mark.parametrize("v", [None, 0, 0.0, -1, float("nan"), float("inf"), -float("inf"), "n/a", "", [1], {}])
def test_usable_price_rejects_everything_else(v):
    assert R._usable_price(v) is False


@pytest.mark.parametrize("bad", BAD_PRICES)
def test_traded_buy_with_unusable_price_is_not_booked(env, bad):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="TRADED", avg=bad, filled=10)]
    t = go(db)
    assert o.status == "PLACED"
    assert positions(db) == []
    assert cash(db) == START_CASH
    assert t["entries_filled"] == 0 and t["errors"] == 0


@pytest.mark.parametrize("bad", [0.0, float("nan")])
def test_part_traded_with_unusable_price_is_not_booked(env, bad):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="PART_TRADED", avg=bad, filled=4)]
    t = go(db)
    assert o.status == "PLACED" and positions(db) == [] and cash(db) == START_CASH
    assert t["partial_fills"] == 0 and t["errors"] == 0


@pytest.mark.parametrize("bad", [0.0, float("nan")])
def test_cancelled_with_filled_shares_and_unusable_price_stays_open(env, bad):
    """Shares traded, then the rest was cancelled: marking it dead would lose the real fill."""
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="CANCELLED", avg=bad, filled=3)]
    t = go(db)
    assert o.status == "PLACED"
    assert positions(db) == [] and cash(db) == START_CASH
    assert t["dead_orders"] == 0 and t["errors"] == 0


def test_cancelled_with_filled_shares_and_good_price_still_books_the_partial(env):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="CANCELLED", avg=100.0, filled=3)]
    t = go(db)
    assert o.status == "CANCELLED"
    assert positions(db)[0].qty_open == 3
    assert t["partial_fills"] == 1


@pytest.mark.parametrize("bad", [0.0, float("nan")])
def test_sell_with_unusable_price_does_not_book_a_total_loss(env, bad):
    db, rig = env
    pos = mk_pos(db, status="PENDING_EXIT", qty=10, avg=100.0)
    o = mk_order(db, side="SELL", dhan_id="S1", exit_reason="stop_hit")
    rig.book = [row(oid="S1", status="TRADED", avg=bad, filled=10, otype="MARKET")]
    t = go(db)
    assert o.status == "PLACED"
    assert pos.qty_open == 10 and pos.status == "PENDING_EXIT"
    assert (pos.realized_pnl or 0.0) == 0.0
    assert cash(db) == START_CASH
    assert t["exits_confirmed"] == 0 and t["errors"] == 0


def test_recovers_once_the_broker_reports_a_real_price(env):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="TRADED", avg=0.0, filled=10)]
    go(db)
    assert o.status == "PLACED" and positions(db) == []
    rig.book = [row(status="TRADED", avg=99.5, filled=10)]
    t = go(db)
    assert o.status == "FILLED"
    assert positions(db)[0].avg_entry_price == 99.5 and positions(db)[0].qty_open == 10
    assert t["entries_filled"] == 1


def test_already_booked_order_still_finalizes_when_price_later_reads_zero(env):
    """Nothing new to book (delta 0): the price is irrelevant, so the terminal status must still be stamped."""
    db, rig = env
    o = mk_order(db, filled_so_far=10)
    rig.book = [row(status="TRADED", avg=0.0, filled=10)]
    t = go(db)
    assert o.status == "FILLED"
    assert t["errors"] == 0
