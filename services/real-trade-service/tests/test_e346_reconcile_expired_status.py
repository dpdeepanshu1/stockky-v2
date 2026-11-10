"""
E346 - reconcile treats Dhan's EXPIRED order status as dead, like REJECTED / CANCELLED.

Every other dead-status list in the service already had EXPIRED; reconcile's did not, so an EXPIRED row looked
"still pending at the broker" for ever: a BUY stayed PLACED, and an EXPIRED SELL stayed in flight (it blocks every other
exit for the symbol and keeps the orphan repair from restoring a PENDING_EXIT position).

Reuses the real-portfolio / scripted-broker harness from test_rt_reconcile.py.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e346_reconcile_expired_status.py -q
"""
from __future__ import annotations

import models
from execution import reconcile as R
from test_rt_reconcile import (  # noqa: F401  (env is a pytest fixture)
    START_CASH, cash, env, mk_order, mk_pos, positions, row, run,
)


def go(db):
    return run(R.reconcile_real_orders(db))


def test_expired_is_a_dead_status():
    assert "EXPIRED" in R._DEAD_STATUSES and "EXPIRED" not in R._FILLED_STATUSES | R._PARTIAL_STATUSES


def test_expired_unfilled_buy_is_closed_out_not_left_placed(env):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="EXPIRED", avg=None, filled=0)]
    t = go(db)
    assert o.status == "EXPIRED" and t["dead_orders"] == 1 and t["errors"] == 0
    assert positions(db) == [] and cash(db) == START_CASH


def test_expired_zero_fill_sell_unsticks_the_position_and_counts_the_failure(env):
    db, rig = env
    pos = mk_pos(db, status="PENDING_EXIT")
    o = mk_order(db, side="SELL", dhan_id="S1", exit_reason="stop_hit")
    rig.book = [row("S1", status="EXPIRED", avg=None, filled=0)]
    t = go(db)
    assert o.status == "EXPIRED" and t["dead_orders"] == 1 and t["positions_unstuck"] == 1
    assert pos.status == "OPEN" and pos.consecutive_exit_failures == 1


def test_an_expired_sell_no_longer_counts_as_in_flight_for_the_orphan_repair(env):
    """Before: the EXPIRED row stayed PLACED, so the repair pass skipped the PENDING_EXIT position for ever."""
    db, rig = env
    pos = mk_pos(db, status="PENDING_EXIT")
    mk_order(db, side="SELL", dhan_id="S1", exit_reason="stop_hit")
    rig.book = [row("S1", status="EXPIRED", avg=None, filled=0)]
    go(db)
    go(db)                                                    # a second cycle must not change anything
    assert pos.status == "OPEN"


def test_expired_after_a_partial_fill_books_the_partial_then_closes_the_order(env):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="EXPIRED", avg=100.0, filled=4)]
    t = go(db)
    assert o.status == "EXPIRED" and positions(db)[0].qty_open == 4 and t["partial_fills"] == 1 and t["dead_orders"] == 1
    assert "after 4 of 10 already filled" in db.query(models.TradeOrderEvent).filter_by(order_id=o.id).order_by(models.TradeOrderEvent.id).all()[-1].detail


def test_expired_with_filled_shares_but_no_price_stays_open_to_retry(env):
    db, rig = env
    o = mk_order(db)
    rig.book = [row(status="EXPIRED", avg=0.0, filled=4)]
    t = go(db)
    assert o.status == "PLACED" and positions(db) == [] and cash(db) == START_CASH and t["dead_orders"] == 0


def test_an_expired_broker_stop_is_not_a_failed_exit(env):
    db, rig = env
    pos = mk_pos(db, status="OPEN")
    o = mk_order(db, side="SELL", dhan_id="S1", exit_reason="broker_stop")
    rig.book = [row("S1", status="EXPIRED", avg=None, filled=0)]
    t = go(db)
    assert o.status == "EXPIRED" and t["positions_unstuck"] == 0 and (pos.consecutive_exit_failures or 0) == 0
