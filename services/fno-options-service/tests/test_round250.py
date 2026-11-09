"""Round 250: (1) an order is sized on the FILL (ask + slippage), so the loss at the stop never exceeds the risk budget;
(2) a DEMO contract that expired before the square-off is booked at its settlement value, not at its entry premium."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from fno import engine as eng, paper, sizing, store as st
from fno.models import ContractRef
from fno.params import Params
from fno.provider import IST
from test_engine_paper import NOW, FakeProvider, engine, make_cfg
from helpers import make_daily
from test_round248 import NEXT, _leftover, db  # noqa: F401  (db is the shared fixture)


# ---- 1. sizing on the fill -------------------------------------------------------------------------------------------------
def test_sizing_on_the_fill_keeps_the_stop_loss_inside_the_budget():
    p = Params(capital=500000, risk_per_trade_pct=1.0, sl_pct=25, max_lots=5, slippage_pct=0.5)
    lot = 65
    budget = p.capital * p.risk_per_trade_pct / 100.0
    for a in (40.0, 55.3, 76.9, 101.25, 133.4, 187.7, 240.0):
        f = paper.buy_fill(a, a, p)
        n = sizing.lots_for(f, lot, p)
        if n:
            assert n * lot * f * p.sl_pct / 100.0 <= budget + 1e-6, (a, f, n)


def test_entry_is_sized_on_the_fill_not_on_the_ask(db):
    probe = engine(db)
    probe.tick(NOW)
    first = db.open_positions()[0]
    ch = FakeProvider().chain("NIFTY", "2026-11-17")
    ask = ch.row(first.strike).side(first.opt_type).ask
    risk_per_lot_at_ask = ask * first.lot_size * Params().sl_pct / 100.0
    budget = 2 * risk_per_lot_at_ask + 0.01                              # exactly two lots at the ask; the fill is dearer, so two lots would break the budget
    pct = budget / Params().capital * 100.0
    s2 = st.Store(":memory:")
    try:
        e = engine(s2, cfg=make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_RISK_PER_TRADE_PCT=pct))
        e.tick(NOW)
        p = s2.open_positions()[0]
        assert p.lots == 1, p.lots                                       # the old code (sized on the ask) gave 2 here
        assert p.qty * (p.entry_premium - p.sl_price) <= budget + 1e-6
    finally:
        s2.close()


def test_the_backtests_size_on_the_fill_too():
    import inspect
    from backtest import eod, rolling_sim, sim
    for mod in (eod, rolling_sim, sim):
        src = inspect.getsource(mod)
        assert "lots_for(fill" in src and "lots_for(ask" not in src and 'lots_for(row["o"]' not in src, mod.__name__


# ---- 2. a DEMO contract that expired before the square-off ------------------------------------------------------------------
class ExpiredProv(FakeProvider):
    def __init__(self, close_on_expiry, expiry_day=datetime(2026, 11, 10, tzinfo=IST), no_candle=False):
        super().__init__()
        self.close_on_expiry, self.expiry_day, self.no_candle = close_on_expiry, expiry_day, no_candle

    def chain(self, u, e):
        return None if e < "2026-11-11" else super().chain(u, e)

    def daily(self, u):
        rows = [] if self.no_candle else [{"t": self.expiry_day.timestamp(), "o": 24480.0, "h": 24600.0, "l": 24400.0, "c": self.close_on_expiry, "v": 1e6}]
        return make_daily(n=5, t0=int(self.expiry_day.timestamp()) - 6 * 86400) + rows


def _expired_leftover(db, opt="CE", strike=24500.0, premium=100.0):
    c = ContractRef("NIFTY", "2026-11-10", strike, opt, 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 2, premium, NOW.replace(hour=9, minute=30).timestamp(), Params(), mode="DEMO")
    db.insert_position(p)
    return p


def test_expired_call_is_booked_at_its_intrinsic_value(db):
    e = engine(db, ExpiredProv(24530.0))
    p = _expired_leftover(db, "CE")
    e.manage_open(NEXT, Params(), {}, {})
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_premium == 30.0           # 24,530 - 24,500
    assert q.net_pnl < 0                                                 # a real loss, not 0


def test_expired_put_out_of_the_money_is_booked_at_one_tick(db):
    e = engine(db, ExpiredProv(24530.0))
    p = _expired_leftover(db, "PE")
    e.manage_open(NEXT, Params(), {}, {})
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_premium == 0.05


def test_expired_contract_without_a_candle_keeps_the_old_price(db):
    e = engine(db, ExpiredProv(24530.0, no_candle=True))
    p = _expired_leftover(db, "CE")
    e.manage_open(NEXT, Params(), {}, {})
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_premium == 100.0


def test_a_contract_that_has_not_expired_is_still_booked_at_the_entry_premium(db):
    pv = ExpiredProv(24530.0)
    pv.chain = lambda u, e: None                                          # no quote for a live contract
    e = engine(db, pv)
    p = _leftover(db, premium=100.0)                                      # expiry 2026-11-17
    e.manage_open(NEXT, Params(), {}, {})
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_premium == 100.0


def test_a_failing_provider_does_not_stop_the_square_off(db):
    pv = ExpiredProv(24530.0)
    def boom(u): raise RuntimeError("down")
    pv.daily = boom
    e = engine(db, pv)
    p = _expired_leftover(db, "CE")
    e.manage_open(NEXT, Params(), {}, {})
    assert db.get_position(p.id).status == paper.CLOSED
