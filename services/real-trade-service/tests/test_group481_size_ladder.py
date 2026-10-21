"""group 481: evidence-gated size ladder (entry_engine/size_ladder.py). Pure + a fake db; no database needed."""
from __future__ import annotations

import os
import sys
import types

import pytest

import config

try:  # the ladder only needs models.TradePnlDaily's columns to build a filter; a fake db ignores them
    import models  # noqa: F401
except Exception:  # pragma: no cover - sandbox without sqlalchemy
    _m = types.ModuleType("models")
    _m.TradePnlDaily = types.SimpleNamespace(mode=types.SimpleNamespace(__eq__=lambda a, b: True), day=types.SimpleNamespace(desc=lambda: None))
    sys.modules["models"] = _m
from entry_engine import size_ladder as sl


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    for k, v in dict(SIZE_LADDER_ENABLED=True, SIZE_LADDER_STEP_PCT=25.0, SIZE_LADDER_MAX_LEVEL=2, SIZE_LADDER_MIN_DAYS=10,
                     SIZE_LADDER_MIN_POSITIVE_DAYS_PCT=60.0, SIZE_LADDER_BRAKE_DAYS=3).items():
        monkeypatch.setattr(config, k, v)
    sl.reset()
    yield
    sl.reset()


class _Row:
    def __init__(self, net, orders=2):
        self.net_realized, self.orders = net, orders


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *a):
        return _Q(list(self.rows))


def test_shipped_defaults_in_config_source():
    src = open(os.path.join(os.path.dirname(config.__file__), "config.py")).read()
    assert '"SIZE_LADDER_ENABLED") or "").strip() or "true"' in src
    for frag in ('"SIZE_LADDER_STEP_PCT") or "").strip() or "25.0"', '"SIZE_LADDER_MAX_LEVEL") or "").strip() or "2"',
                 '"SIZE_LADDER_MIN_DAYS") or "").strip() or "10"', '"SIZE_LADDER_MIN_POSITIVE_DAYS_PCT") or "").strip() or "60.0"',
                 '"SIZE_LADDER_BRAKE_DAYS") or "").strip() or "3"'):
        assert frag in src


def test_no_history_stays_at_level_zero():
    assert sl.level_for([])[0] == 0
    assert sl.level_for([50.0] * 9)[0] == 0          # one day short of level 1


def test_level_one_needs_positive_total_and_enough_positive_days():
    assert sl.level_for([20.0] * 10)[0] == 1
    mixed = [30.0, 30.0, 30.0, 30.0, 30.0, -5.0, -5.0, -5.0, -5.0, -5.0]     # 50 % positive days < 60 %
    assert sl.level_for(mixed)[0] == 0
    assert sl.level_for([30.0, 30.0, 30.0, 30.0, 30.0, 30.0, -5.0, -5.0, -5.0, -5.0])[0] == 1   # 60 %
    assert sl.level_for([5.0] * 6 + [-100.0] * 4)[0] == 0                                          # negative total


def test_level_two_needs_twenty_days():
    assert sl.level_for([20.0] * 20)[0] == 2
    assert sl.level_for([20.0] * 15)[0] == 1
    assert sl.level_for([20.0] * 10 + [-50.0] * 10)[0] == 1          # newest 10 earn level 1, the 20-day total is negative


def test_brake_drops_to_zero_when_the_newest_days_lose():
    assert sl.level_for([-30.0, 5.0, 5.0] + [20.0] * 20)[0] == 0
    assert sl.level_for([-30.0, 40.0, 5.0] + [20.0] * 20)[0] == 2
    assert "brake" in sl.level_for([-30.0, 5.0, 5.0] + [20.0] * 20)[1]


def test_max_level_zero_disables_growth(monkeypatch):
    monkeypatch.setattr(config, "SIZE_LADDER_MAX_LEVEL", 0)
    assert sl.level_for([20.0] * 30)[0] == 0


def test_cap_math_and_never_below_base():
    db = _Db([_Row(20.0) for _ in range(12)])
    st = sl.status(db, "REAL", 3000.0)
    assert st["level"] == 1 and st["cap"] == 3750.0
    sl.reset()
    assert sl.effective_cap(_Db([_Row(20.0) for _ in range(25)]), "REAL", 3000.0) == pytest.approx(4687.5)
    sl.reset()
    assert sl.effective_cap(_Db([_Row(-9.0) for _ in range(25)]), "REAL", 3000.0) == 3000.0


def test_days_without_orders_or_net_do_not_count():
    rows = [_Row(None)] + [_Row(20.0, orders=0) for _ in range(12)] + [_Row(20.0) for _ in range(5)]
    assert sl.status(_Db(rows), "REAL", 3000.0)["level"] == 0


def test_demo_and_disabled_and_bad_base_use_the_plain_cap(monkeypatch):
    db = _Db([_Row(20.0) for _ in range(25)])
    assert sl.effective_cap(db, "DEMO", 3000.0) == 3000.0
    assert sl.effective_cap(db, "REAL", None) is None
    monkeypatch.setattr(config, "SIZE_LADDER_ENABLED", False)
    sl.reset()
    assert sl.effective_cap(db, "REAL", 3000.0) == 3000.0


def test_a_database_error_returns_the_base_cap():
    class Boom:
        def query(self, *a):
            raise RuntimeError("db down")
    assert sl.effective_cap(Boom(), "REAL", 3000.0) == 3000.0


def test_cache_is_per_mode_and_resets_when_the_base_changes():
    db = _Db([_Row(20.0) for _ in range(12)])
    assert sl.effective_cap(db, "REAL", 3000.0) == 3750.0
    assert sl.effective_cap(_Db([]), "REAL", 3000.0) == 3750.0          # served from the 5-minute cache
    assert sl.effective_cap(_Db([]), "REAL", 2000.0) == 2000.0          # a different base is recomputed


def test_wired_into_the_automatic_entry_snapshot_and_status_route():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    entry = open(os.path.join(root, "entry_engine", "entry.py")).read()
    assert "size_ladder.effective_cap(" in entry and "from entry_engine import size_ladder" in entry
    assert "size_ladder.status(db" in open(os.path.join(root, "main.py")).read()
