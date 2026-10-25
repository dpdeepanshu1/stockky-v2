"""E138: swing lane wiring - the exit-engine backstop, the EOD-switch entry gate, the /swing/status wiring block, the Oracle-safe boolean filters.

Part 1 (pure rules, stdlib only) runs anywhere. Part 2 needs SQLAlchemy (in-memory SQLite + the real models) and runs under pytest on the VM, inside Docker (the host Python has no SQLAlchemy):
    scripts/docker_test.sh real-trade-service tests/test_swing_e138_wiring.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
sys.path.insert(0, SVC)

import config  # noqa: E402
from swing import lane, live  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def ist(y, m, d, h=11, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def bv(**k):
    base = dict(sessions=2, max_hold=5, opened_day=date(2026, 10, 26), today=date(2026, 10, 27), past_cutoff=False, swing_live=True)
    return lane.backstop_verdict(**{**base, **k})


# ───────────────────────────── part 1: pure rules ─────────────────────────────

def test_backstop_verdict_rules():
    assert bv()[0] is False                                              # mid-hold, live: leave it to the stop and the 15:00 decision
    assert bv(sessions=6)[0] is True                                     # outlived its last session
    assert bv(sessions=5)[0] is False                                    # last session, square-off not yet due
    assert bv(sessions=5, past_cutoff=True)[0] is True                   # last session and the square-off missed it
    assert bv(swing_live=False)[0] is True                               # carried leftover with the lane not live
    assert bv(swing_live=False, opened_day=date(2026, 10, 27))[0] is False   # bought today: not a leftover
    for bad in (dict(opened_day=None), dict(sessions=0), dict(sessions=None), dict(max_hold="x")):
        assert bv(**bad)[0] is False, bad                                # doubt = no sale


def test_overdue_uses_the_real_calendar_and_the_cutoff(monkeypatch):
    monkeypatch.setattr(config, "SWING_MODE", "live")
    monkeypatch.setattr(config, "SWING_MAX_HOLD_DAYS", 5)
    monkeypatch.setattr(config, "EOD_SQUAREOFF_TIME_IST", "15:00")
    pos = SimpleNamespace(entry_decision_label="SWING_MOMENTUM", opened_at=datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc))   # Wed 21 Oct; Tue 20 is a holiday
    assert live.overdue(pos, ist(2026, 10, 26, 11)) is None              # session 4
    assert live.overdue(pos, ist(2026, 10, 27, 15, 9)) is None           # session 5, before 15:10
    assert "last session" in live.overdue(pos, ist(2026, 10, 27, 15, 10))
    assert "past the 5-session limit" in live.overdue(pos, ist(2026, 10, 28, 9, 30))
    monkeypatch.setattr(config, "SWING_MODE", "shadow")
    assert "not live" in live.overdue(pos, ist(2026, 10, 26, 11))
    other = SimpleNamespace(entry_decision_label="OTHER", opened_at=pos.opened_at)
    assert live.overdue(other, ist(2026, 10, 28, 9, 30)) is None         # never touches a non-swing position
    assert live.overdue(SimpleNamespace(entry_decision_label="SWING_MOMENTUM", opened_at=None), ist(2026, 10, 28)) is None


def test_backstop_cutoff_follows_the_square_off_time_and_never_passes_1525(monkeypatch):
    monkeypatch.setattr(config, "EOD_SQUAREOFF_TIME_IST", "15:00")
    assert (live.backstop_cutoff().hour, live.backstop_cutoff().minute) == (15, 10)
    monkeypatch.setattr(config, "EOD_SQUAREOFF_TIME_IST", "15:20")
    assert (live.backstop_cutoff().hour, live.backstop_cutoff().minute) == (15, 25)
    monkeypatch.setattr(config, "EOD_SQUAREOFF_TIME_IST", "garbage")
    assert (live.backstop_cutoff().hour, live.backstop_cutoff().minute) == (15, 10)


# ───────────────────────────── part 2: database tests (VM) ─────────────────────────────

@pytest.fixture()
def db():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    for m in ("DEMO", "REAL"):
        s.add(models.TradeAccount(mode=m, starting_capital=100000.0, current_equity=100000.0, cash_available=100000.0))
    s.commit()
    yield s
    s.close()


def gate(db, mode="DEMO", armed=True, eod=True):
    import models
    g = models.TradeGateState(mode=mode, armed=armed, eod_squareoff_enabled=eod)
    db.add(g)
    db.commit()
    return g


def swing_pos(db, *, mode="DEMO", symbol="SWINGCO", opened=None, stop=95.0, label="SWING_MOMENTUM"):
    import models
    p = models.TradePosition(mode=mode, symbol=symbol, status="OPEN", qty_open=10, avg_entry_price=100.0, current_stop=stop, current_target=None,
                             initial_stop_distance=5.0, entry_decision_label=label, opened_at=opened or datetime.now(timezone.utc))
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def quotes(monkeypatch, mapping):
    import exit_engine.exit as ex

    async def _q(symbols, **_kw):
        return dict(mapping)
    monkeypatch.setattr(ex, "get_quotes", _q)


def tick(price, symbol="SWINGCO"):
    from market_feed.feed import Tick
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=None, source="test", day_high=None, day_low=None)


def closed_reason(db, pos):
    """The reason portfolio.close_position wrote on the CLOSED event ("<reason>: <qty> @ <price> (pnl ...)")."""
    import models
    ev = db.query(models.TradePositionEvent).filter_by(position_id=pos.id, event_type="CLOSED").first()
    return ev.detail.split(":")[0] if ev else None


def run(coro):
    return asyncio.run(coro)


def test_exit_tick_sells_a_swing_position_the_square_off_never_sold(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    p = swing_pos(db, opened=datetime.now(timezone.utc) - timedelta(days=30))
    quotes(monkeypatch, {"SWINGCO": tick(101.0)})
    tally = run(ex.evaluate_mode(db, "DEMO"))
    db.refresh(p)
    assert tally["full_exits"] == 1 and p.status == "CLOSED"
    assert closed_reason(db, p) == "swing_max_hold"


def test_exit_tick_stop_still_wins_over_the_backstop(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    p = swing_pos(db, opened=datetime.now(timezone.utc) - timedelta(days=30))
    quotes(monkeypatch, {"SWINGCO": tick(94.0)})
    run(ex.evaluate_mode(db, "DEMO"))
    assert closed_reason(db, p) == "stop_hit"


def test_exit_tick_leaves_a_position_bought_today_alone(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    p = swing_pos(db)
    quotes(monkeypatch, {"SWINGCO": tick(101.0)})
    tally = run(ex.evaluate_mode(db, "DEMO"))
    db.refresh(p)
    assert tally["held"] == 1 and tally["full_exits"] == 0 and p.status == "OPEN"


def test_exit_tick_sells_a_carried_leftover_when_the_lane_is_not_live(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "shadow")
    old = swing_pos(db, symbol="OLDCO", opened=datetime.now(timezone.utc) - timedelta(days=3))
    new = swing_pos(db, symbol="NEWCO")
    quotes(monkeypatch, {"OLDCO": tick(101.0, "OLDCO"), "NEWCO": tick(101.0, "NEWCO")})
    run(ex.evaluate_mode(db, "DEMO"))
    db.refresh(old)
    db.refresh(new)
    assert old.status == "CLOSED" and new.status == "OPEN"


def test_exit_tick_never_applies_the_backstop_to_a_normal_position(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    p = swing_pos(db, label="OPENING_RANGE", opened=datetime.now(timezone.utc) - timedelta(days=30))
    p.current_target = 200.0
    db.commit()
    quotes(monkeypatch, {"SWINGCO": tick(101.0)})
    run(ex.evaluate_mode(db, "DEMO"))
    assert closed_reason(db, p) != "swing_max_hold"


def test_real_swing_position_goes_through_send_real_sell_with_the_backstop_reason(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    swing_pos(db, mode="REAL", opened=datetime.now(timezone.utc) - timedelta(days=30))
    calls = []
    monkeypatch.setattr(ex, "_send_real_sell", lambda db_, position, qty, reason, **kw: calls.append((position.symbol, qty, reason)) or True)
    quotes(monkeypatch, {"SWINGCO": tick(101.0)})
    tally = run(ex.evaluate_mode(db, "REAL"))
    assert calls == [("SWINGCO", 10, "swing_max_hold")] and tally["full_exits"] == 1


def test_a_refused_real_sale_is_retried_on_the_next_tick(db, monkeypatch):
    import exit_engine.exit as ex
    monkeypatch.setattr(config, "SWING_MODE", "live")
    swing_pos(db, mode="REAL", opened=datetime.now(timezone.utc) - timedelta(days=30))
    monkeypatch.setattr(ex, "_send_real_sell", lambda *a, **k: False)
    quotes(monkeypatch, {"SWINGCO": tick(101.0)})
    assert run(ex.evaluate_mode(db, "REAL"))["held"] == 1
    calls = []
    monkeypatch.setattr(ex, "_send_real_sell", lambda db_, position, qty, reason, **kw: calls.append(reason) or True)
    run(ex.evaluate_mode(db, "REAL"))
    assert calls == ["swing_max_hold"]


def test_live_entries_refuse_to_start_without_the_eod_switch(db, monkeypatch):
    from swing.repo import SqlRepo
    monkeypatch.setattr(config, "SWING_MODE", "live")
    gate(db, "DEMO", armed=True, eod=False)
    repo = SqlRepo(db)
    res = run(live.entries(db, repo, "DEMO", True))
    assert res["status"] == "eod_squareoff_off" and res["placed"] == 0
    from tz_utils import ist_today_str
    assert repo.has_run("entry", ist_today_str(), "DEMO", ok_only=False) is False      # the day's one entry run was not used up


def test_live_entries_get_past_the_eod_gate_when_it_is_on(db, monkeypatch):
    from swing.repo import SqlRepo
    monkeypatch.setattr(config, "SWING_MODE", "live")
    gate(db, "DEMO", armed=True, eod=True)
    assert run(live.entries(db, SqlRepo(db), "DEMO", True))["status"] != "eod_squareoff_off"


def test_eod_squareoff_on_reads_the_gate_row(db):
    assert live.eod_squareoff_on(db, "DEMO") is False                    # no row
    gate(db, "DEMO", eod=True)
    gate(db, "REAL", eod=False)
    assert live.eod_squareoff_on(db, "DEMO") is True and live.eod_squareoff_on(db, "REAL") is False


def _problems(db, now):
    from swing.repo import SqlRepo
    return live.wiring(db, SqlRepo(db), now)["problems"]


def test_wiring_is_clean_apart_from_scan_and_loop_when_everything_is_on(db, monkeypatch):
    monkeypatch.setattr(config, "SWING_MODE", "live")
    gate(db, "DEMO", armed=True, eod=True)
    gate(db, "REAL", armed=False, eod=False)
    rest = [p for p in _problems(db, ist(2026, 10, 26)) if "schedule loop" not in p and "evening scan" not in p]
    assert rest == []


def test_wiring_flags_live_entries_without_the_gate_switches(db, monkeypatch):
    monkeypatch.setattr(config, "SWING_MODE", "live")
    gate(db, "DEMO", armed=False, eod=False)
    text = " | ".join(_problems(db, ist(2026, 10, 26)))
    assert "DEMO: the gate is not armed" in text and "DEMO: the gate's EOD square-off switch is off" in text and "live entries are refused" in text


def test_wiring_flags_an_open_swing_position_without_the_eod_switch_and_real_without_edis(db, monkeypatch):
    import models
    monkeypatch.setattr(config, "SWING_MODE", "live")
    monkeypatch.setattr(config, "SWING_LIVE_REAL_ENABLED", True)
    gate(db, "DEMO", armed=True, eod=True)
    gate(db, "REAL", armed=True, eod=False)
    db.query(models.TradeGateState).filter_by(mode="REAL").update({"edis_morning_check_enabled": False})
    db.commit()
    swing_pos(db, mode="REAL", symbol="HELD")
    text = " | ".join(_problems(db, ist(2026, 10, 26)))
    assert "REAL: the gate's EOD square-off switch is off" in text and "backstop" in text and "eDIS morning check is off" in text


def test_wiring_says_nothing_about_the_lane_when_it_is_off(db, monkeypatch):
    monkeypatch.setattr(config, "SWING_MODE", "off")
    assert _problems(db, ist(2026, 10, 26)) == []


def test_run_filters_use_the_oracle_safe_boolean_form_and_still_filter(db):
    from sqlalchemy import select
    from sqlalchemy.dialects import oracle
    import models
    from swing.repo import SqlRepo
    repo = SqlRepo(db)
    repo.add_run("scan", "2026-10-26", None, False, "failed scan", "{}")
    assert repo.has_run("scan", "2026-10-26") is False                   # a failed run does not count
    assert repo.has_run("scan", "2026-10-26", ok_only=False) is True
    repo.add_run("scan", "2026-10-26", None, True, "good scan", "{}")
    assert repo.has_run("scan", "2026-10-26") is True
    assert repo.latest_run("scan")["summary"] == "good scan"
    sql = str(select(models.SwingRun.id).where(models.SwingRun.ok == True).compile(dialect=oracle.dialect()))   # noqa: E712
    assert sql.rstrip().endswith("= 1") and " IS 1" not in sql
    for rel in ("swing/repo.py", "main.py"):
        src = open(os.path.join(SVC, rel), encoding="utf-8").read()
        assert "SwingRun.ok.is_(" not in src and "R.ok.is_(" not in src, rel
