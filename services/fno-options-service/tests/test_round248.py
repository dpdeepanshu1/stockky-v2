"""Round 248: a position still open from an EARLIER day counts in today's gates and is closed at the next open (this service is intraday only)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from fno import engine as eng, gates, paper, store as st
from fno.models import ContractRef
from fno.params import Params
from fno.provider import IST
from test_engine_paper import NOW, FakeProvider, engine, make_cfg
from test_real_partial import RB, real, sells

NEXT = NOW + timedelta(days=1)                      # Wednesday 11 Nov 2026, 10:30 IST


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _leftover(db, mode="DEMO", opened=NOW, premium=None):
    """An open NIFTY CE. `premium` defaults to what the fake chain quotes now, so neither the stop nor the target is hit."""
    if premium is None:
        ch = FakeProvider().chain("NIFTY", "2026-11-17")
        premium = ch.row(24500.0).ce.mid
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 1, premium, opened.timestamp(), Params(), mode=mode)
    db.insert_position(p)
    return p


def _prov(at):
    pv = FakeProvider()
    pv.now_ts = at.timestamp()
    return pv


def test_carried_over_rule():
    assert gates.carried_over("2026-11-10", datetime(2026, 11, 11, 9, 15, tzinfo=IST))
    assert not gates.carried_over("2026-11-10", datetime(2026, 11, 11, 9, 14, tzinfo=IST))          # market not open yet
    assert not gates.carried_over("2026-11-11", datetime(2026, 11, 11, 11, 0, tzinfo=IST))          # opened today
    assert not gates.carried_over("2026-11-13", datetime(2026, 11, 14, 11, 0, tzinfo=IST))          # a Saturday
    assert not gates.carried_over("2026-11-10", datetime(2026, 11, 11, 11, 0, tzinfo=IST), holidays=["2026-11-11"])


def test_open_position_from_an_earlier_day_counts_in_todays_gates(db):
    _leftover(db)
    s = db.day_summary(st.day_of(NEXT.timestamp()), "DEMO")
    assert s["open"] == 1 and s["carried_open"] == 1 and s["trades"] == 0 and s["open_same_side"] == {"NIFTY:CE": 1}
    ds = gates.DayState(trades_today=s["trades"], open_positions=s["open"], open_same_side=s["open_same_side"])
    ok, why = gates.entry_gates(NEXT, date(2026, 11, 17), 14.0, 1.0, ds, Params(max_open_positions=1), key="NIFTY:CE")
    assert not ok and any("positions open" in w for w in why) and any("already holding NIFTY:CE" in w for w in why)
    ok2, why2 = gates.entry_gates(NEXT, date(2026, 11, 17), 14.0, 1.0, ds, Params(max_open_positions=2), key="NIFTY:PE")
    assert not ok2 and any("opposite side" in w for w in why2)                                      # a leftover CALL also blocks a PUT (no hedging by accident)


def test_summary_of_the_day_itself_other_modes_and_past_days_is_unchanged(db):
    _leftover(db)
    s0 = db.day_summary(st.day_of(NOW.timestamp()), "DEMO")
    assert s0["open"] == 1 and s0["carried_open"] == 0 and s0["trades"] == 1
    assert db.day_summary(st.day_of(NEXT.timestamp()), "REAL")["open"] == 0
    _leftover(db, opened=NEXT)
    assert db.day_summary(st.day_of(NOW.timestamp()), "DEMO")["open"] == 1                           # a later position never shows in an earlier day's report
    assert db.day_summary(st.day_of(NEXT.timestamp()), "DEMO")["open"] == 2


def test_leftover_is_closed_at_the_next_open_but_not_before(db):
    e = engine(db, _prov(NEXT))
    p = _leftover(db)
    e.tick(NEXT.replace(hour=9, minute=10))
    assert db.get_position(p.id).status == paper.OPEN
    e.tick(NEXT.replace(hour=9, minute=20))
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_reason == paper.SQUAREOFF


def test_leftover_is_left_alone_on_a_holiday(db):
    e = eng.Engine(db, _prov(NEXT), make_cfg(FNO_UNDERLYINGS="NIFTY"), holidays=("2026-11-11",))
    p = _leftover(db)
    e.tick(NEXT)
    assert db.get_position(p.id).status == paper.OPEN


def test_a_position_opened_today_is_not_treated_as_a_leftover(db):
    e = engine(db, _prov(NEXT))
    p = _leftover(db, opened=NEXT.replace(hour=9, minute=16))
    e.tick(NEXT.replace(hour=9, minute=20))
    assert db.get_position(p.id).status == paper.OPEN


def test_real_leftover_sends_a_sell_when_armed_that_day(db):
    b = RB()
    e = real(db, b)
    e.provider = _prov(NEXT)
    p = _leftover(db, mode="REAL")
    p.security_id, p.segment = 111, "NSE_FNO"
    db.update_position(p)
    db.kv_set("fno_armed_day", "2026-11-11")
    e.tick(NEXT)
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


def test_real_leftover_is_not_sold_when_not_armed_that_day(db):
    b = RB()
    e = real(db, b)                                                                                  # armed for 10 Nov only
    e.provider = _prov(NEXT)
    p = _leftover(db, mode="REAL")
    p.security_id, p.segment = 111, "NSE_FNO"
    db.update_position(p)
    e.tick(NEXT)                                                                                     # round 294: no ARM today and Dhan's positions cannot be read: Dhan may have squared it off, a SELL could open a short
    assert not sells(b) and db.get_position(p.id).status == paper.OPEN
    assert [x for x in db.events("real_blocked")]                                                    # it says so instead of staying silent
    b.net = {"111": p.qty}                                                                           # Dhan still shows it: closing what we hold needs no ARM
    e.tick(NEXT + timedelta(seconds=10))
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED

