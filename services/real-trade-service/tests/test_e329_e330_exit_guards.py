"""
E329 + E330 - exit.py guards around a live SELL, and the emergency gap-down / time-stop checks that silently switched themselves off.

E329  A SELL Dhan accepted must never be followed by a second SELL because recording the first one failed. Bookkeeping is its own
      guarded step: roll back and retry once; if that fails too, hold this position's exits (in-process) and alert, and report the
      SELL as sent (True).
E330  (a) the "SELL sent" alert is best-effort: it formats unrealized_pnl, and a None there used to raise AFTER the SELL was live and
          recorded, which fell into the rejection handler and returned False (the partial-exit caller then never lowered the target).
      (b) emergency gap-down: only a None initial_stop_distance fell back; 0 / NaN / missing stop disabled the check without a word.
      (c) time-stop: a NaN current_target made `ltp < target` False forever.

Run from services/real-trade-service:  python -m pytest tests/test_e329_e330_exit_guards.py -q
"""
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from execution import dhan_client
import exit_engine.exit as ex

_engine = create_engine("sqlite:///:memory:")


def _mkposition(db, **kw):
    d = dict(mode="REAL", symbol="TESTSTOCK", status="OPEN", qty_open=10, avg_entry_price=100.0,
             opened_at=datetime.now(timezone.utc), broker_imported=True, current_stop=95.0,
             current_target=115.0, unrealized_pnl=250.0, initial_stop_distance=5.0)
    d.update(kw)
    pos = models.TradePosition(**d)
    db.add(pos)
    db.commit()
    return pos


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()
    sent = []
    monkeypatch.setattr(ex, "notify_sync", lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(dhan_client, "get_security_id", lambda db_, sym: "1234")
    monkeypatch.setattr(dhan_client, "round_to_tick", lambda p: round(p, 2))
    monkeypatch.setattr(ex.shared_order_budget, "record_order_unconditional", lambda db_: None)
    monkeypatch.setattr(ex, "record_real_exit_sent", lambda *a, **k: None)
    monkeypatch.setattr(ex, "_UNRECORDED_SELLS", {})
    placed = []

    def _place(*a, **k):
        placed.append(k)
        return {"orderId": f"DHAN-{len(placed)}"}
    monkeypatch.setattr(dhan_client, "place_order", _place)
    db._sent = sent
    db._placed = placed
    return db


def _sell(db, pos, reason="stop_hit", **kw):
    return ex._send_real_sell(db, pos, 10, reason, **kw)


# ----------------------------- E329 -----------------------------

def test_recording_failure_retries_once_and_succeeds(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    calls = {"n": 0}
    real_flush = db.flush

    def _flaky_flush(*a, **k):
        if any(isinstance(o, models.TradeOrder) for o in db.new):   # only the SELL's own bookkeeping flush, not attribute-load autoflushes
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
        return real_flush(*a, **k)
    monkeypatch.setattr(db, "flush", _flaky_flush)
    assert _sell(db, pos) is True
    assert len(db._placed) == 1, "exactly one SELL at Dhan"
    assert db.query(models.TradeOrder).filter_by(dhan_order_id="DHAN-1").count() == 1
    assert pos.id not in ex._UNRECORDED_SELLS


def test_recording_failing_twice_holds_exits_and_returns_true(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    pid = pos.id

    real_flush = db.flush

    def _dead_flush(*a, **k):
        if any(isinstance(o, models.TradeOrder) for o in db.new):
            raise RuntimeError("connection lost")
        return real_flush(*a, **k)
    monkeypatch.setattr(db, "flush", _dead_flush)
    assert _sell(db, pos) is True, "the SELL WAS sent, so the caller must not treat it as a rejection"
    assert len(db._placed) == 1
    assert pid in ex._UNRECORDED_SELLS
    assert any("NOT recorded" in m for m in db._sent)
    # the next attempt for the same position is held: no second SELL at Dhan (even once the database is back)
    monkeypatch.setattr(db, "flush", real_flush)
    assert _sell(db, pos) is False
    assert len(db._placed) == 1, "no second SELL while the first one is unrecorded"


def test_hold_expires(env, monkeypatch):
    db = env
    pos = _mkposition(db)
    ex._UNRECORDED_SELLS[pos.id] = ex.time.monotonic() - ex._UNRECORDED_SELL_HOLD_S - 1
    assert _sell(db, pos) is True
    assert len(db._placed) == 1 and pos.id not in ex._UNRECORDED_SELLS


def test_budget_counter_failure_does_not_fail_the_sell(env, monkeypatch):
    db = env
    pos = _mkposition(db)

    def _boom(db_):
        raise RuntimeError("budget table gone")
    monkeypatch.setattr(ex.shared_order_budget, "record_order_unconditional", _boom)
    assert _sell(db, pos) is True
    assert db.query(models.TradeOrder).filter_by(dhan_order_id="DHAN-1").count() == 1


# ----------------------------- E330 (a) -----------------------------

def test_sent_alert_failure_does_not_turn_a_live_sell_into_a_rejection(env, monkeypatch):
    db = env
    pos = _mkposition(db, unrealized_pnl=None)

    def _bad_notify(msg, *a, **k):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(ex, "notify_sync", _bad_notify)
    assert _sell(db, pos) is True
    assert db.query(models.TradeOrder).filter_by(dhan_order_id="DHAN-1").count() == 1
    assert (pos.consecutive_exit_failures or 0) == 0


def test_none_unrealized_pnl_still_sends_the_alert(env):
    db = env
    pos = _mkposition(db, unrealized_pnl=None)
    assert _sell(db, pos) is True
    assert any("SELL sent" in m and "₹0.00" in m for m in db._sent)


# ----------------------------- E330 (b) -----------------------------

class _P:  # minimal stand-in: _emergency_risk_distance only reads attributes
    def __init__(self, isd=None, entry=100.0, stop=95.0):
        self.initial_stop_distance, self.avg_entry_price, self.current_stop = isd, entry, stop


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), "x", None])
def test_unusable_initial_distance_falls_back_to_the_stop(bad):
    d, src = ex._emergency_risk_distance(_P(isd=bad, entry=100.0, stop=96.0))
    assert d == pytest.approx(4.0) and src == "current_stop"


def test_stored_distance_wins():
    assert ex._emergency_risk_distance(_P(isd=3.0)) == (3.0, "initial_stop_distance")


@pytest.mark.parametrize("stop", [None, float("nan"), 0.0, 100.0])
def test_no_usable_distance_is_reported_not_silent(stop):
    d, src = ex._emergency_risk_distance(_P(isd=float("nan"), entry=100.0, stop=stop))
    assert d is None and src == "none"


def test_percent_floor_when_configured(monkeypatch):
    monkeypatch.setattr(ex, "EMERGENCY_NO_RISK_LOSS_PCT", 4.0)
    d, src = ex._emergency_risk_distance(_P(isd=None, entry=200.0, stop=None))
    assert d == pytest.approx(8.0) and src == "pct_floor"


def test_percent_floor_ignores_nan_setting(monkeypatch):
    monkeypatch.setattr(ex, "EMERGENCY_NO_RISK_LOSS_PCT", float("nan"))
    assert ex._emergency_risk_distance(_P(isd=None, entry=200.0, stop=None)) == (None, "none")


# ----------------------------- E330 (c) -----------------------------

def test_usable_positive():
    assert ex._usable_positive(3) == 3.0 and ex._usable_positive("2.5") == 2.5
    for bad in (None, 0, -1, float("nan"), float("inf"), "abc", True is None):
        assert ex._usable_positive(bad) is None
    assert not math.isnan(ex._usable_positive(1e-9))


def test_time_stop_target_survives_a_nan_target():
    # the expression used in evaluate_mode: NaN target falls back to entry * 1.005, so a stalled position can still time out
    entry = 100.0
    target = ex._usable_positive(float("nan")) or entry * 1.005
    assert target == pytest.approx(100.5) and 100.2 < target


def test_source_wires_the_guards():
    src = open(ex.__file__, encoding="utf-8").read()
    assert "_emergency_risk_distance(position)" in src
    assert "_usable_positive(position.current_target) or position.avg_entry_price * 1.005" in src
    assert "_usable_positive(_max_hold) or MAX_HOLD_DAYS" in src
    assert "for _pe_try in (1, 2):" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
