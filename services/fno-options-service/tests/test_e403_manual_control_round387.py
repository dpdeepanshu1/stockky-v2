"""Round 387 (E403): manual control + a fixed F&O amount for REAL sizing.
pause / resume (no new entries, open positions still managed), close ONE position, fixed capital, no REAL entry while an open position has no premium."""
from __future__ import annotations

import pytest

from fno import capital, engine as eng, paper
from fno import store as st
from test_engine_paper import C, NOW, FakeBroker, FakeProvider, make_cfg


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _engine(db, **cfg):
    return eng.Engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY", **cfg))


def _open(db):
    p = paper.open_position(C, 1, 100.0, NOW.timestamp(), eng.Params())
    p.id = db.insert_position(p)
    return p


def test_fixed_capital_overrides_the_board():
    cfg = make_cfg(FNO_REAL_FIXED_CAPITAL=4000.0, CAPITAL_ALLOC_MODE="off")
    amt, why = capital.real_capital(cfg)
    assert amt == 4000.0 and "FNO_REAL_FIXED_CAPITAL" in why
    assert capital.real_capital(make_cfg(FNO_REAL_FIXED_CAPITAL=0.0, CAPITAL_ALLOC_MODE="off"))[0] == 0.0          # 0 = off: the old fail-closed rule stands
    assert capital.real_capital(make_cfg(FNO_REAL_FIXED_CAPITAL=float("nan")))[1] != "fixed F&O amount (FNO_REAL_FIXED_CAPITAL)"       # NaN is not an amount


def test_pause_stops_new_entries_but_not_the_cycle(db):
    e = _engine(db)
    assert e.is_paused() is False
    db.kv_set("fno_paused", True)
    assert e.is_paused() is True
    out = e.tick(NOW)
    assert out["paused"] is True and out["entries"] == [] and out["decisions"] == {}
    db.kv_set("fno_paused", None)
    assert e.is_paused() is False


def test_pause_still_manages_an_open_position(db):
    e = _engine(db)
    p = _open(db)
    db.kv_set("fno_paused", True)
    e.tick(NOW.replace(hour=15, minute=20))                  # after the square-off time: the open position is closed although entries are paused
    assert [x.status for x in db.positions("DEMO")][0] == paper.CLOSED and p.id


def test_close_one_position_only(db):
    e = _engine(db)
    a, b = _open(db), _open(db)
    out = e.close_position(a.id, now=NOW)
    assert out["closed"] is True and [x.id for x in db.open_positions()] == [b.id]
    assert "error" in e.close_position(9999, now=NOW)
    assert "error" in e.close_position(a.id, now=NOW)         # already closed: not an open position any more


def test_real_entry_waits_while_a_position_has_no_premium(db):
    class NoQuote(FakeProvider):
        def chain(self, u, e):
            return None
    e = eng.Engine(db, NoQuote(), make_cfg(FNO_UNDERLYINGS="NIFTY"))
    _open(db)
    e.tick(NOW)
    assert e._unpriced                                         # the open position could not be priced this cycle
