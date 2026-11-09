"""Round 249: the loss of a position left over from an earlier day counts against the day it is closed in (the daily loss limit)."""
from __future__ import annotations

from datetime import date

from fno import gates, paper, store as st
from fno.params import Params
from fno.models import ContractRef
from test_engine_paper import NOW, engine
from test_round248 import NEXT, _leftover, _prov, db  # noqa: F401  (db is the shared fixture)


# ---- round 249: the loss of a leftover that closes today counts against today's loss limit ----------------------------------------
def test_leftover_loss_counts_against_todays_loss_limit(db):
    e = engine(db, _prov(NEXT))
    p = _leftover(db, premium=100.0)                                   # entered on 10 Nov at 100; the fake chain quotes far above or below it
    paper.close_position(p, 40.0, NEXT.replace(hour=9, minute=16).timestamp(), paper.SQUAREOFF, Params())
    db.update_position(p)
    day = st.day_of(NEXT.timestamp())
    s = db.day_summary(day, "DEMO")
    assert s["carried_closed_net"] == p.net_pnl and p.net_pnl < 0
    assert s["realized_net"] == 0.0 and s["trades"] == 0                # reports of the day itself are unchanged
    assert db.day_summary(st.day_of(NOW.timestamp()), "DEMO")["carried_closed_net"] == 0.0      # and the entry day's report does not gain it twice
    assert e.day_state(NEXT, "DEMO").realized_pnl_net == p.net_pnl


def test_leftover_loss_can_trip_the_daily_loss_gate(db):
    e = engine(db, _prov(NEXT))
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 2, 200.0, NOW.timestamp(), Params(), mode="DEMO")
    db.insert_position(p)
    paper.close_position(p, 80.0, NEXT.replace(hour=9, minute=16).timestamp(), paper.SQUAREOFF, Params())      # -120 x 130 = -15,600 gross: over 3 % of Rs 5,00,000 (15,000)
    db.update_position(p)
    ok, why = gates.entry_gates(NEXT, date(2026, 11, 17), 14.0, 1.0, e.day_state(NEXT, "DEMO"), Params(), key="NIFTY:CE")
    assert not ok and any("daily loss limit" in w for w in why)


def test_a_position_closed_on_its_own_day_is_not_carried(db):
    p = _leftover(db, premium=100.0)
    paper.close_position(p, 60.0, NOW.replace(hour=11).timestamp(), paper.SL, Params())
    db.update_position(p)
    assert db.day_summary(st.day_of(NOW.timestamp()), "DEMO")["carried_closed_net"] == 0.0
    assert db.day_summary(st.day_of(NEXT.timestamp()), "DEMO")["carried_closed_net"] == 0.0
    assert db.day_summary("not-a-day", "DEMO")["carried_closed_net"] == 0.0
