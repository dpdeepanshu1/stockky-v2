"""E285 round 95: the Real Trade ladder's two missing halves - the daily-loss brake (loss_days_hit, from trade_pnl_daily) and the per-trade risk multiple (risk_mult_for).
DB stubbed (no SQLAlchemy needed). Plain runner: python3 tests/test_e285_rt_ladder_risk.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import risk_ladder, risk_stage as rs


class _Col:
    def __eq__(self, o):          # noqa: D105 - column == value is just a truthy stand-in
        return True
    def __hash__(self):
        return 1
    def desc(self):
        return self


def _fake_models(day_rows, equity=10000.0, limit_pct=3.0):
    m = types.ModuleType("models")
    m.TradePnlDaily = SimpleNamespace(mode=_Col(), day=_Col())
    m.TradeAccount = SimpleNamespace(mode=_Col())
    m.TradeRiskConfig = SimpleNamespace(mode=_Col())

    class Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *a, **k): return self
        def filter_by(self, **k): return self
        def order_by(self, *a): return self
        def limit(self, n): return self
        def all(self): return list(self.rows)
        def first(self): return self.rows[0] if self.rows else None

    class DB:
        def query(self, model):
            if model is m.TradePnlDaily: return Q(day_rows)
            if model is m.TradeAccount: return Q([SimpleNamespace(current_equity=equity)] if equity is not None else [])
            if model is m.TradeRiskConfig: return Q([SimpleNamespace(max_daily_loss_pct=limit_pct)] if limit_pct is not None else [])
            return Q([])
    return m, DB()


def _days(*nets, orders=3):
    return [SimpleNamespace(net_realized=n, orders=orders) for n in nets]


def _with_models(day_rows, **kw):
    m, db = _fake_models(day_rows, **kw)
    sys.modules["models"] = m
    return db


def _restore():
    sys.modules.pop("models", None)


def test_loss_days_two_of_five_hit():
    try:
        # equity 10,000 and a 3 % limit = Rs 300; two of the last five days lost 300 or more
        db = _with_models(_days(-320.0, 50.0, -300.0, 20.0, 10.0, -900.0))
        assert rs.loss_days_hit(db) is True                      # the sixth (oldest) day is outside the five
        db = _with_models(_days(-320.0, 50.0, -299.0, 20.0, 10.0))
        assert rs.loss_days_hit(db) is False                     # only one day reached the limit
        db = _with_models(_days(-320.0, 50.0))
        assert rs.loss_days_hit(db) is False                     # fewer days than hits need
    finally:
        _restore()


def test_loss_days_counts_only_captured_days_and_fails_quiet():
    try:
        rows = _days(-400.0, -400.0) + [SimpleNamespace(net_realized=None, orders=2), SimpleNamespace(net_realized=-999.0, orders=0)]
        assert rs.day_nets(_with_models(rows)) == [-400.0, -400.0]          # a NULL net and a no-order day are not days
        assert rs.loss_days_hit(_with_models(_days(-400.0, -400.0), equity=None)) is False   # no account row = no pool = no brake
        assert rs.loss_days_hit(_with_models(_days(-400.0, -400.0), equity=0.0)) is False
        # no risk row: DEFAULT_MAX_DAILY_LOSS_PCT (3 %) is used
        assert rs.loss_days_hit(_with_models(_days(-400.0, -400.0), limit_pct=None)) is True
        config.RISK_LADDER_LOSS_DAYS_ENABLED = False
        assert rs.loss_days_hit(_with_models(_days(-400.0, -400.0))) is False
        config.RISK_LADDER_LOSS_DAYS_ENABLED = True
        assert rs.loss_days_hit(None) is False                                # a broken db never raises
    finally:
        config.RISK_LADDER_LOSS_DAYS_ENABLED = True
        _restore()


def test_loss_days_drops_one_stage_through_stage_for():
    saved = rs._data
    try:
        good = {"r_n": 150, "lower_bound_r": 0.1}                            # stage 1
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good}, "last": [], "loss_days": False}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 1
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good}, "last": [], "loss_days": True}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 0                      # the loss days drop it
        assert rs.stage_for(None, "S9") == 0                                  # unknown setup stays 0 (never below 0)
        two = {"r_n": 400, "lower_bound_r": 0.3}                              # stage 2 -> 1 with loss days
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": two}, "last": [], "loss_days": True}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 1
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": good}, "last": []}   # an old cache without the key = no brake
        assert rs.stage_for(None, "S1_CATALYST_UP") == 1
    finally:
        rs._data = saved


def test_risk_mult_by_stage_and_switches():
    saved = rs._data
    try:
        cell = lambda n, lb: {"cells": {"S1_CATALYST_UP|ALL": {"r_n": n, "lower_bound_r": lb}}, "last": [], "loss_days": False}
        rs._data = lambda db: cell(10, 0.5)
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0               # stage 0 = today's size
        rs._data = lambda db: cell(150, 0.1)
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.25               # stage 1 = 2 % -> 2.5 %
        rs._data = lambda db: cell(400, 0.3)
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.5                # stage 2 = 3 %
        rs._data = lambda db: {**cell(400, 0.3), "older": {"S1_CATALYST_UP|ALL": {"r_n": 400, "lower_bound_r": 0.3}}}
        assert rs.stage_for(None, "S1_CATALYST_UP") == 3 and rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.5   # stage 3 risks like stage 2 (3 % cap)
        assert rs.risk_mult_for(None, None) == 1.0 and rs.risk_mult_for(None, "S9") == 1.0
        config.RISK_LADDER_RISK_ENABLED = False
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0
        config.RISK_LADDER_RISK_ENABLED = True
        config.RISK_LADDER_ENABLED = False                                    # the master switch also ends it
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0
        config.RISK_LADDER_ENABLED = True
        def boom(db):
            raise RuntimeError("db down")
        rs._data = boom
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0                # any error = base risk
    finally:
        rs._data = saved
        config.RISK_LADDER_RISK_ENABLED = True
        config.RISK_LADDER_ENABLED = True


def test_step_is_configurable_and_never_lowers():
    saved, step = rs._data, config.RISK_LADDER_RISK_STEP
    try:
        rs._data = lambda db: {"cells": {"S1_CATALYST_UP|ALL": {"r_n": 150, "lower_bound_r": 0.1}}, "last": [], "loss_days": False}
        config.RISK_LADDER_RISK_STEP = 0.5
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.5
        config.RISK_LADDER_RISK_STEP = -1.0                                   # a negative step can never size below the base
        assert rs.risk_mult_for(None, "S1_CATALYST_UP") == 1.0
    finally:
        rs._data = saved
        config.RISK_LADDER_RISK_STEP = step


def test_entry_applies_the_multiple_only_for_real_and_after_the_setup_size():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entry_engine", "entry.py"), encoding="utf-8").read()
    i = src.index("adj_risk_pct *= _setup_mult")
    j = src.index("risk_mult_for", i)
    k = src.index("max_trade_risk = account_state.equity * (adj_risk_pct / 100.0)")
    assert i < j < k                                                          # applied after the setup multiplier and before the quantity is sized
    assert 'if mode == "REAL"' in src[i:j]


def test_switches_in_config_and_catalog():
    from control import catalog
    names = {f.key for f in catalog.CATALOG}
    assert {"RISK_LADDER_RISK_ENABLED", "RISK_LADDER_LOSS_DAYS_ENABLED"} <= names
    assert config.RISK_LADDER_RISK_ENABLED is True and config.RISK_LADDER_LOSS_DAYS_ENABLED is True and config.RISK_LADDER_RISK_STEP == 0.25
    assert risk_ladder.LOSS_DAY_HITS == 2 and risk_ladder.LOSS_DAY_SESSIONS == 5


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print(f"{n} passed")
