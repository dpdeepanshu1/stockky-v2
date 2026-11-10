"""Round 300: capital.stats counts TRADES (not rows) for the capital board's min_trades gate: NOT_FILLED is not a trade, a partial-exit child belongs to its parent's trade."""
from __future__ import annotations

import pytest

from fno import capital, paper, store as st
from fno.models import ContractRef
from fno.params import Params
from test_engine_paper import NOW, make_cfg

TODAY = st.day_of(NOW.timestamp())


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _row(db, lots=2):
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, lots, 100.0, NOW.timestamp(), Params(), mode="REAL")
    db.insert_position(p)
    return p


def _close(db, p, reason, net):
    p.status, p.exit_ts, p.exit_reason = paper.CLOSED, NOW.timestamp() + 60, reason
    p.gross_pnl, p.charges, p.net_pnl = net, 0.0, net
    db.update_position(p)


def _stats(db):
    return capital.stats(db, make_cfg(FNO_UNDERLYINGS="NIFTY"), today=TODAY)


def test_an_unfilled_entry_is_not_a_trade(db):
    _close(db, _row(db), "NOT_FILLED", 0.0)
    _close(db, _row(db), "TARGET", 500.0)
    s = _stats(db)
    assert s["trades"] == 1 and s["net_pnl"] == 500.0


def test_a_partial_exit_family_is_one_trade_once_all_closed(db):
    parent = _row(db, lots=2)
    child = paper.split_closed(parent, 1, 130.0, NOW.timestamp() + 30, "TARGET1", Params())
    db.insert_position(child)
    s = _stats(db)
    assert s["trades"] == 0 and s["net_pnl"] == child.net_pnl       # the parent is still open: no trade yet, the sold lot's money already counts
    parent.lots = 1
    _close(db, parent, "SL", -200.0)
    s = _stats(db)
    assert s["trades"] == 1 and abs(s["net_pnl"] - (child.net_pnl - 200.0)) < 0.01


def test_plain_closed_trades_are_counted_as_before(db):
    _close(db, _row(db), "TARGET", 100.0)
    _close(db, _row(db), "SL", -50.0)
    s = _stats(db)
    assert s["trades"] == 2 and s["net_pnl"] == 50.0
