"""Round 298: (#11) the runner keeps managing what is open until 16:00 but a SELL after the close needs Dhan's net position; (#9) a token that says it is not an admin is refused,
while the login tokens real-trade / position-stocks issue (only `sub`) still work. Fake broker / provider, no network."""
from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta

import pytest

from fno import gates, paper, runtime, store as st
from fno.provider import IST
from test_engine_paper import NOW
from test_real_partial import RB, entered, real, sells

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def at(h, m):
    return NOW.replace(hour=h, minute=m)


# ---- time helpers -----------------------------------------------------------------------------------------------------------
def test_after_close_edges():
    assert not gates.after_close(at(9, 15)) and not gates.after_close(at(15, 31)) and not gates.after_close(at(11, 0))
    assert gates.after_close(at(15, 32)) and gates.after_close(at(18, 0)) and gates.after_close(at(9, 14)) and gates.after_close(at(2, 0))


def test_management_window_is_after_the_close_until_sixteen_hundred_on_trading_days():
    assert not runtime.management_open(at(15, 31)) and runtime.management_open(at(15, 32)) and runtime.management_open(at(16, 0))
    assert not runtime.management_open(at(16, 1)) and not runtime.management_open(at(11, 0))
    assert not runtime.management_open(NOW.replace(hour=15, minute=40), holidays=("2026-11-10",))
    sat = datetime(2026, 11, 14, 15, 40, tzinfo=IST)
    assert not runtime.management_open(sat)


# ---- the SELL proof after the close -----------------------------------------------------------------------------------------
def test_after_the_close_no_sell_when_dhan_positions_cannot_be_read(db):
    b = RB(net=None); e = real(db, b); p = entered(db, b, e)
    n = len(sells(b))
    e.tick(at(15, 40), manage_only=True)
    assert len(sells(b)) == n and db.get_position(p.id).status == paper.OPEN
    assert any(x["kind"] == "real_blocked" and "after the close" in str(x) for x in db.events())


def test_after_the_close_no_sell_when_dhan_shows_the_contract_flat(db):
    b = RB(net={}); e = real(db, b); p = entered(db, b, e)
    e.tick(at(15, 40), manage_only=True)
    assert not sells(b) and db.get_position(p.id).status == paper.OPEN
    ev = [x for x in db.events() if x["kind"] == "real_blocked"]
    assert ev and "closed there already" in str(ev[-1])


def test_after_the_close_it_sells_when_dhan_still_shows_the_position(db):
    b = RB(); e = real(db, b); p = entered(db, b, e)
    b.net = {str(p.security_id): p.qty}
    e.tick(at(15, 40), manage_only=True)
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


def test_a_manual_squareoff_after_the_close_needs_the_same_proof(db):
    b = RB(net={}); e = real(db, b); p = entered(db, b, e)
    assert e.squareoff_all(now=at(18, 0)) == [] and not sells(b) and db.get_position(p.id).status == paper.OPEN
    b.net = {str(p.security_id): p.qty}
    assert e.squareoff_all(now=at(18, 5)) == [p.id] and sells(b)


def test_during_market_hours_the_squareoff_needs_no_extra_proof(db):
    b = RB(net=None); e = real(db, b); p = entered(db, b, e)
    e.tick(at(15, 20))                                                  # 15:15 square-off, broker positions unreadable: the sell still goes (as before)
    assert sells(b) and db.get_position(p.id).status == paper.CLOSED


def test_the_blocked_note_is_throttled_not_one_row_per_cycle(db):
    b = RB(net={}); e = real(db, b); p = entered(db, b, e)
    for i in range(6):
        e.tick(at(15, 40) + timedelta(seconds=5 * i), manage_only=True)
    assert len([x for x in db.events() if x["kind"] == "real_blocked" and "after the close" in str(x)]) == 1


# ---- manage_only never analyses or enters -----------------------------------------------------------------------------------
def test_manage_only_makes_no_entry_where_a_normal_tick_does(db):
    b = RB(); e = real(db, b)
    e.tick(NOW, manage_only=True)
    assert not db.open_positions("REAL") and not b.sent
    e.tick(NOW)
    assert db.open_positions("REAL")                                    # the same inputs do enter on a normal cycle


# ---- the runner -------------------------------------------------------------------------------------------------------------
def _run_once(monkeypatch, e, when, tmp_path):
    r = runtime.Runner(e, e.cfg, str(tmp_path))
    calls = []
    monkeypatch.setattr(runtime, "now_ist", lambda: when)
    monkeypatch.setattr(runtime.capital, "publish", lambda *a, **k: None)
    monkeypatch.setattr(runtime.readiness, "publish", lambda *a, **k: None)
    monkeypatch.setattr(e, "tick", lambda now=None, manage_only=False: (calls.append(manage_only), r._stop.set()))
    monkeypatch.setattr(r, "snapshot", lambda now: calls.append("snap"))
    r._loop()
    return calls


def test_runner_manages_open_positions_after_the_close_and_does_not_record(db, monkeypatch, tmp_path):
    b = RB(); e = real(db, b); entered(db, b, e)
    assert _run_once(monkeypatch, e, at(15, 45), tmp_path) == [True]


def test_runner_is_idle_after_the_close_with_nothing_open_and_after_sixteen_hundred(db, monkeypatch, tmp_path):
    b = RB(); e = real(db, b)
    r = runtime.Runner(e, e.cfg, str(tmp_path))
    monkeypatch.setattr(runtime, "now_ist", lambda: at(15, 45))
    monkeypatch.setattr(runtime.capital, "publish", lambda *a, **k: None)
    monkeypatch.setattr(runtime.readiness, "publish", lambda *a, **k: None)
    monkeypatch.setattr(e, "tick", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not tick")))
    monkeypatch.setattr(r._stop, "wait", lambda t: r._stop.set())
    r._loop()                                                           # nothing open: no tick
    entered_b = RB(); e2 = real(db, entered_b); entered(db, entered_b, e2)
    r2 = runtime.Runner(e2, e2.cfg, str(tmp_path))
    monkeypatch.setattr(runtime, "now_ist", lambda: at(16, 5))
    monkeypatch.setattr(e2, "tick", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not tick")))
    monkeypatch.setattr(r2._stop, "wait", lambda t: r2._stop.set())
    r2._loop()


def test_runner_in_market_hours_still_does_the_full_cycle(db, monkeypatch, tmp_path):
    b = RB(); e = real(db, b)
    assert _run_once(monkeypatch, e, at(11, 0), tmp_path) == [False, "snap"]


# ---- #9 is_admin -----------------------------------------------------------------------------------------------------------
def _is_admin():
    import auth
    return auth.is_admin


@pytest.mark.parametrize("claims,ok", [
    ({"sub": "deepanshu"}, True),                                       # what real-trade / position-stocks issue at login
    ({"sub": "deepanshu", "iat": 1, "exp": 2}, True),
    ({"sub": "svc", "role": "admin", "admin": True}, True),             # the calibration-runner service token
    ({"role": "admin"}, True), ({"admin": True}, True),
    ({"sub": "x", "admin": False}, False), ({"sub": "x", "admin": 0}, False), ({"sub": "x", "admin": "false"}, False),
    ({"sub": "x", "role": "viewer"}, False), ({"sub": "x", "role": "user", "admin": True}, False),
    ({}, False), ({"sub": ""}, False), ({"sub": "   "}, False), ({"sub": None}, False), ({"sub": 7}, False),
])
def test_is_admin(claims, ok):
    assert _is_admin()(claims) is ok


def test_both_copies_of_is_admin_are_identical():
    def src(path):
        t = ast.parse(open(path).read())
        return ast.get_source_segment(open(path).read(), next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == "is_admin"))
    assert src(os.path.join(HERE, "auth.py")) == src(os.path.join(HERE, "..", "calibration-runner-service", "auth.py"))
