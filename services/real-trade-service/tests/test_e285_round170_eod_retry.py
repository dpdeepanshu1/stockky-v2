"""Round 170 (C1): the EOD square-off retry (execution/eod_retry.py + auto_pilot._eod_squareoff_retry + the schedule-tick wiring).

The 15:00 square-off runs once a day; a Dhan MARKET sell that was cancelled or rejected (28 and 29 Oct) was never sent again.
Pure decisions are tested directly; the auto_pilot parts run on an in-memory database like test_auto_pilot_orchestration.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import auto_pilot as ap
from execution import eod_retry as er
from resilience.local_cache import load_snapshot, save_snapshot

T = dict(interval_s=120.0, max_tries=5, until=dtime(15, 20))
_engine = create_engine("sqlite:///:memory:")
DAY = "2026-11-04"


def run(coro):
    return asyncio.run(coro)


class P:
    def __init__(self, id, symbol, qty_open=5):
        self.id, self.symbol, self.qty_open = id, symbol, qty_open


class TestTiming:
    def test_due_after_interval(self):
        assert er.retry_due(now_ist=dtime(15, 3), now_ts=1130, last_ts=1000, tries=0, **T)

    def test_not_due_inside_interval(self):
        assert not er.retry_due(now_ist=dtime(15, 1), now_ts=1100, last_ts=1000, tries=0, **T)

    def test_not_due_when_tries_used_up(self):
        assert not er.retry_due(now_ist=dtime(15, 10), now_ts=5000, last_ts=1000, tries=5, **T)

    def test_not_due_after_cutoff_but_due_at_it(self):
        assert not er.retry_due(now_ist=dtime(15, 21), now_ts=5000, last_ts=1000, tries=1, **T)
        assert er.retry_due(now_ist=dtime(15, 20), now_ts=5000, last_ts=1000, tries=1, **T)

    def test_alert_rules(self):
        k = dict(alerted=False, **T)
        assert not er.alert_due(now_ist=dtime(15, 10), now_ts=5000, last_ts=1000, tries=2, **k)     # tries remain
        assert not er.alert_due(now_ist=dtime(15, 12), now_ts=1050, last_ts=1000, tries=5, **k)     # fill not yet booked
        assert er.alert_due(now_ist=dtime(15, 12), now_ts=1130, last_ts=1000, tries=5, **k)
        assert er.alert_due(now_ist=dtime(15, 22), now_ts=5000, last_ts=1000, tries=2, **k)         # cut-off passed
        assert not er.alert_due(now_ist=dtime(15, 22), now_ts=5000, last_ts=1000, tries=5, alerted=True, **T)

    def test_owed_only_plan_ids_with_shares(self):
        ps = [P(1, "A"), P(2, "B"), P(3, "C", 0), P(4, "D")]
        assert [p.symbol for p in er.owed(ps, [1, 3, 4])] == ["A", "D"]

    def test_plan_is_json_friendly(self):
        assert er.new_plan({3, 1}, 10.5) == {"ids": [1, 3], "tries": 0, "last": 10.5, "alerted": False}


class Rig:
    def __init__(self):
        self.open = [P(1, "AAA"), P(2, "BBB")]
        self.pending, self.sells, self.notes, self.ok = set(), [], [], True

    async def go(self, plan, now_ts=1130.0, now_ist=dtime(15, 3)):
        async def notify(m):
            self.notes.append(m)

        def send(p, q):
            self.sells.append((p.symbol, q))
            return self.ok

        return await er.run_pass(mode="REAL", plan=plan, load_open=lambda: self.open, has_pending=lambda s: s in self.pending,
                                 send_sell=send, notify=notify, now_ts=now_ts, now_ist=now_ist, **T)


def plan(ids=(1, 2), tries=0, last=1000.0, alerted=False):
    return {"ids": list(ids), "tries": tries, "last": last, "alerted": alerted}


class TestRunPass:
    def test_sends_again_and_counts(self):
        r = Rig()
        out = run(r.go(plan()))
        assert r.sells == [("AAA", 5), ("BBB", 5)] and out["tries"] == 1 and out["last"] == 1130.0
        assert "EOD retry 1" in r.notes[0]

    def test_waits_for_interval(self):
        r = Rig()
        out = run(r.go(plan(last=1100.0)))
        assert r.sells == [] and r.notes == [] and out["tries"] == 0

    def test_only_plan_positions_so_an_overnight_hold_is_never_sold(self):
        r = Rig()
        run(r.go(plan(ids=[1])))
        assert [s[0] for s in r.sells] == ["AAA"]

    def test_pending_sell_is_skipped_but_still_uses_the_try(self):
        r = Rig()
        r.pending = {"AAA"}
        out = run(r.go(plan()))
        assert [s[0] for s in r.sells] == ["BBB"] and out["tries"] == 1

    def test_flat_book_does_nothing(self):
        r = Rig()
        r.open = []
        out = run(r.go(plan()))
        assert r.sells == [] and r.notes == [] and out["tries"] == 0

    def test_failed_send_is_reported_and_exception_does_not_escape(self):
        r = Rig()
        r.ok = False
        out = run(r.go(plan()))
        assert "2 could not be sent" in r.notes[0]

        r2 = Rig()
        async def notify(m): r2.notes.append(m)
        def boom(p, q): raise RuntimeError("dhan down")
        out = run(er.run_pass(mode="REAL", plan=plan(), load_open=lambda: r2.open, has_pending=lambda s: False, send_sell=boom,
                              notify=notify, now_ts=1130.0, now_ist=dtime(15, 3), **T))
        assert out["tries"] == 1 and "2 could not be sent" in r2.notes[0]

    def test_cap_then_one_alert(self):
        r = Rig()
        out = run(r.go(plan(tries=5, last=1000.0)))
        assert r.sells == [] and len(r.notes) == 1 and "NOT flat" in r.notes[0] and "AAA x5" in r.notes[0] and out["alerted"] is True
        run(r.go(out))
        assert len(r.notes) == 1

    def test_no_alert_right_after_last_try(self):
        r = Rig()
        run(r.go(plan(tries=5, last=1100.0)))
        assert r.notes == []

    def test_nothing_sent_after_cutoff_only_alert(self):
        r = Rig()
        out = run(r.go(plan(tries=1), now_ts=9000.0, now_ist=dtime(15, 25)))
        assert r.sells == [] and len(r.notes) == 1 and out["alerted"]


# ---------------------------------------------------------------- auto_pilot, on an in-memory database
@pytest.fixture()
def db(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    monkeypatch.setattr(ap, "ist_today_str", lambda *a, **k: DAY)
    monkeypatch.setattr(ap, "ist_now", lambda *a, **k: datetime(2026, 11, 4, 9, 33, tzinfo=timezone.utc))   # 15:03 IST
    monkeypatch.setattr(config, "EOD_RETRY_ENABLED", True)
    monkeypatch.setattr(config, "EOD_RETRY_INTERVAL_SEC", 120)
    monkeypatch.setattr(config, "EOD_RETRY_MAX_TRIES", 5)
    monkeypatch.setattr(config, "EOD_RETRY_UNTIL_IST", "15:20")
    yield s
    s.close()


def _pos(db, symbol, mode="REAL", **kw):
    d = dict(mode=mode, symbol=symbol, status="OPEN", qty_open=5, avg_entry_price=100.0, opened_at=datetime.now(timezone.utc), realized_pnl=0.0)
    d.update(kw)
    p = models.TradePosition(**d)
    db.add(p)
    db.commit()
    return p


def _notes(monkeypatch):
    sink = []

    async def fake(msg, *a, **k):
        sink.append(msg)

    monkeypatch.setattr(ap, "notify_async", fake)
    return sink


class TestFirstRunStoresPlan:
    def _run(self, db, monkeypatch, mode, holds):
        monkeypatch.setattr(ap, "_select_overnight_holds", lambda *a, **k: _co((set(holds), {i: "kept" for i in holds})))
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda d, s: False)
        monkeypatch.setattr("exit_engine.exit._send_real_sell", lambda d, p, q, r, full: True)
        monkeypatch.setattr("market_feed.feed.get_quotes", lambda *a, **k: _co({}))
        _notes(monkeypatch)
        run(ap._eod_squareoff(db, mode))

    def test_real_stores_ids_without_the_overnight_holds(self, db, monkeypatch):
        a, b = _pos(db, "AAA"), _pos(db, "BBB")
        self._run(db, monkeypatch, "REAL", {b.id})
        got = load_snapshot(db, er.plan_key("REAL", DAY))
        assert got["ids"] == [a.id] and got["tries"] == 0 and got["alerted"] is False

    def test_demo_stores_nothing(self, db, monkeypatch):
        _pos(db, "AAA", mode="DEMO")
        self._run(db, monkeypatch, "DEMO", set())
        assert load_snapshot(db, er.plan_key("DEMO", DAY)) is None

    def test_all_held_stores_nothing(self, db, monkeypatch):
        a = _pos(db, "AAA")
        self._run(db, monkeypatch, "REAL", {a.id})
        assert load_snapshot(db, er.plan_key("REAL", DAY)) is None


def _co(v):
    async def _f():
        return v
    return _f()


class TestRetryInAutoPilot:
    def _setup(self, db, monkeypatch, sell_ok=True):
        sells = []
        monkeypatch.setattr("exit_engine.exit._has_pending_real_sell", lambda d, s: False)
        monkeypatch.setattr("exit_engine.exit._send_real_sell", lambda d, p, q, r, full: sells.append((p.symbol, q, r, full)) or sell_ok)
        monkeypatch.setattr(ap, "_time_mod", type("T", (), {"time": staticmethod(lambda: 1130.0)}))
        return sells, _notes(monkeypatch)

    def test_resends_for_a_plan_position_and_survives_a_restart(self, db, monkeypatch):
        a, b = _pos(db, "AAA"), _pos(db, "BBB")
        sells, notes = self._setup(db, monkeypatch)
        save_snapshot(db, er.plan_key("REAL", DAY), plan(ids=[a.id], last=1000.0))   # B is an overnight hold: not in the plan
        run(ap._eod_squareoff_retry(db, "REAL"))
        assert sells == [("AAA", 5, "eod_squareoff", True)]
        st = load_snapshot(db, er.plan_key("REAL", DAY))
        assert st["tries"] == 1 and st["last"] == 1130.0
        run(ap._eod_squareoff_retry(db, "REAL"))      # same instant: interval not over, nothing more (state came from the database)
        assert len(sells) == 1

    def test_no_plan_no_retry_no_alert(self, db, monkeypatch):
        _pos(db, "AAA")
        sells, notes = self._setup(db, monkeypatch)
        run(ap._eod_squareoff_retry(db, "REAL"))
        assert sells == [] and notes == []

    def test_demo_and_switch_off_do_nothing(self, db, monkeypatch):
        a = _pos(db, "AAA")
        sells, notes = self._setup(db, monkeypatch)
        save_snapshot(db, er.plan_key("REAL", DAY), plan(ids=[a.id]))
        run(ap._eod_squareoff_retry(db, "DEMO"))
        monkeypatch.setattr(config, "EOD_RETRY_ENABLED", False)
        run(ap._eod_squareoff_retry(db, "REAL"))
        assert sells == [] and notes == []

    def test_cap_reached_then_alert_once_stored(self, db, monkeypatch):
        a = _pos(db, "AAA")
        sells, notes = self._setup(db, monkeypatch)
        save_snapshot(db, er.plan_key("REAL", DAY), plan(ids=[a.id], tries=5, last=1000.0))
        run(ap._eod_squareoff_retry(db, "REAL"))
        run(ap._eod_squareoff_retry(db, "REAL"))
        assert sells == [] and len(notes) == 1 and "AAA x5" in notes[0]


class TestTickWiring:
    def test_source_pins(self):
        src = (Path(ap.__file__)).read_text(encoding="utf-8")
        i = src.index("EOD square-off retry (E285 round 170")
        block = src[i:i + 1100]
        assert 'mode == "REAL"' in block and "eod_squareoff_last_run" in block and "== today" in block
        assert "acquire(blocking=False)" in block and "_eod_squareoff_retry(db, mode)" in block and "is_market_open_ist(honor_session_proof=False)" in block
        assert src.index("EOD square-off retry (E285 round 170") > src.index("await _eod_squareoff(db, mode)")

    def test_tick_runs_retry_only_after_todays_squareoff(self, db, monkeypatch):
        for last, want in ((DAY, 1), ("2026-11-03", 0)):
            db.query(models.TradeGateState).delete()
            db.add(models.TradeGateState(mode="REAL", armed=True, eod_squareoff_enabled=True, eod_squareoff_last_run=last))
            db.commit()
            calls = []
            monkeypatch.setattr(ap, "get_session_factory", lambda: sessionmaker(bind=_engine))
            monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t: False)      # the 15:00 run itself does not fire in this test
            monkeypatch.setattr(ap, "is_market_open_ist", lambda *a, **k: True)
            monkeypatch.setattr(ap, "_eod_squareoff_retry", lambda d, m: calls.append(m) or _co(None))
            run(ap._schedule_tick_body("REAL"))
            assert len(calls) == want
            assert ap._get_exit_lock("REAL").acquire(blocking=False) is True
            ap._get_exit_lock("REAL").release()
