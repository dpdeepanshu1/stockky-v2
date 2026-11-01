"""E275 round 37: peak-time record (excursion), per-cell typical gain / time-to-peak (setup_stats), shrinkage, and the hook using them.
Stubs sqlalchemy / models / dhan_client only while loading, like test_e275_momentum_hook."""
import importlib.util
import json
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0


def _stub_modules(monkeypatch):
    sa, sa_orm = types.ModuleType("sqlalchemy"), types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sa.orm = sa_orm
    models = types.ModuleType("models")
    models.ScalpPosition = SimpleNamespace(status=object())
    for n, m in (("sqlalchemy", sa), ("sqlalchemy.orm", sa_orm), ("models", models)):
        monkeypatch.setitem(sys.modules, n, m)


@pytest.fixture()
def excursion(monkeypatch):
    _stub_modules(monkeypatch)
    spec = importlib.util.spec_from_file_location("excursion_under_test", ROOT / "orders" / "excursion.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDB:
    def __init__(self, rows):
        self.rows, self.commits = rows, 0

    def query(self, *a):
        return self

    def filter(self, *a):
        return self

    def all(self):
        return self.rows

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_excursion_records_when_the_best_prices_were_first_seen(excursion, monkeypatch):
    from datetime import datetime, timezone
    pos = SimpleNamespace(id=1, symbol="X", entry_price=100.0, opened_at=datetime.fromtimestamp(T0, timezone.utc), max_price_seen=None, min_price_seen=None, flow_json=None)
    ticks = [(T0 + 10, 101.0), (T0 + 120, 103.0), (T0 + 200, 103.0), (T0 + 300, 99.0), (T0 + 400, 102.0)]
    feed = types.ModuleType("feed.ws_client")
    feed.get_tick_buffer = lambda sym: ticks
    monkeypatch.setitem(sys.modules, "feed.ws_client", feed)
    import feed as feed_pkg
    monkeypatch.setattr(feed_pkg, "ws_client", feed, raising=False)
    assert excursion.run_excursion_tracking(FakeDB([pos])) == 1
    pk = json.loads(pos.flow_json)["peak"]
    assert pos.max_price_seen == 103.0 and pk["hi_at"] == T0 + 120 and pk["lo_at"] == T0 + 300      # first tick at the high, not the repeat at +200
    ticks.append((T0 + 500, 104.0))
    excursion.run_excursion_tracking(FakeDB([pos]))
    pk = json.loads(pos.flow_json)["peak"]
    assert pk["hi_at"] == T0 + 500 and pk["lo_at"] == T0 + 300                                      # only the improved side moves


def test_stamp_peak_keeps_other_keys_and_survives_garbage(excursion):
    out = json.loads(excursion.stamp_peak(json.dumps({"setup": {"id": "S1"}}), hi_at=5.0))
    assert out["setup"]["id"] == "S1" and out["peak"] == {"hi_at": 5.0}
    assert excursion.stamp_peak("not json", hi_at=1.0) == "not json" or json.loads(excursion.stamp_peak("not json", hi_at=1.0))
    assert excursion.stamp_peak("[1,2]", hi_at=1.0) == "[1,2]"


# ── setup_stats ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def trade(i, setup="S1_CATALYST_UP", slot="0930", win=True, gain=1.5, ttp=12.0, short=False):
    fj = {"setup": {"id": setup, "slot": slot, "side": "SHORT" if short else "LONG"}, "peak": {("lo_at" if short else "hi_at"): T0 + ttp * 60.0}}
    e = 100.0
    best = e * (1 - gain / 100) if short else e * (1 + gain / 100)
    return {"flow_json": json.dumps(fj), "realized_pnl": 10.0 if win else -10.0, "entry_price": e, "stop_price": 99.0, "quantity": 10,
            "opened_at": f"2026-10-{1 + i % 8:02d} 09:30:00", "opened_ts": T0, "side": "SHORT" if short else "LONG",
            "max_price_seen": e if short else best, "min_price_seen": best if short else e}


def test_cell_gets_median_gain_and_time_to_peak_from_winners_only():
    from orders import setup_stats as ss
    rows = [trade(i, gain=1.0 + 0.1 * (i % 5), ttp=10.0 + (i % 5)) for i in range(30)] + [trade(100 + i, win=False, gain=9.0, ttp=55.0) for i in range(5)]
    c = ss.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["gain_n"] == 30 and c["ttp_n"] == 30 and c["typical_gain_pct"] == 1.2 and c["ttp_min"] == 12.0      # losers (9 %, 55 min) are not in it


def test_short_reads_the_lowest_price_and_the_low_time():
    from orders import setup_stats as ss
    rows = [trade(i, short=True, gain=2.0, ttp=7.0) for i in range(30)]
    c = ss.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["typical_gain_pct"] == 2.0 and c["ttp_min"] == 7.0


def test_too_few_samples_or_missing_peak_give_none():
    from orders import setup_stats as ss
    rows = [trade(i) for i in range(30)]
    for r in rows:
        r["flow_json"] = json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930"}})                  # no peak recorded (older trades)
    c = ss.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["ttp_min"] is None and c["ttp_n"] == 0 and c["typical_gain_pct"] == 1.5
    few = ss.summarize([trade(i) for i in range(30)][:9] + [trade(50 + i, win=False) for i in range(21)])["S1_CATALYST_UP|0930"]
    assert few["gain_n"] == 9 and few["typical_gain_pct"] is None


def test_absurd_or_negative_peak_times_are_ignored():
    from orders import setup_stats as ss
    rows = [trade(i, ttp=-5.0) for i in range(15)] + [trade(20 + i, ttp=900.0) for i in range(15)]
    assert ss.summarize(rows)["S1_CATALYST_UP|0930"]["ttp_n"] == 0


def test_shrinkage_toward_the_constants():
    from orders import setup_stats as ss
    cell = {"n": 100, "ttp_n": 30, "ttp_min": 10.0, "gain_n": 30, "typical_gain_pct": 2.0}
    o = ss.exit_numbers(cell, ttp_fallback_min=20.0, target_pct=3.0)
    assert o["ttp_min"] == 15.0 and o["typical_gain_pct"] == 2.5 and o["ttp_source"].startswith("learned")      # w = 30 / 60 = 0.5
    big = ss.exit_numbers({**cell, "ttp_n": 270, "ttp_min": 10.0}, ttp_fallback_min=20.0)
    assert big["ttp_min"] == 11.0                                                                               # w = 0.9
    thin = ss.exit_numbers({**cell, "n": 29}, ttp_fallback_min=20.0, target_pct=3.0)
    assert thin["ttp_min"] == 20.0 and thin["typical_gain_pct"] == 3.0 and thin["ttp_source"] == "fallback"
    assert ss.exit_numbers(None, ttp_fallback_min=20.0)["ttp_min"] == 20.0
    assert ss.exit_numbers({"n": "x"}, ttp_fallback_min=20.0)["ttp_source"] == "fallback"
    nobase = ss.exit_numbers(cell, ttp_fallback_min=20.0, target_pct=None)
    assert nobase["typical_gain_pct"] == 2.0


# ── the hook uses them ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def hook(monkeypatch):
    _stub_modules(monkeypatch)
    dhan = types.ModuleType("execution.dhan_client")
    ex = types.ModuleType("execution")
    ex.dhan_client = dhan
    monkeypatch.setitem(sys.modules, "execution", ex)
    monkeypatch.setitem(sys.modules, "execution.dhan_client", dhan)
    spec = importlib.util.spec_from_file_location("momentum_hook_learned_test", ROOT / "orders" / "momentum_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pos(stamp=True):
    fj = json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930"}}) if stamp else None
    return SimpleNamespace(flow_json=fj)


def test_hook_takes_the_cells_numbers_clamped(hook, monkeypatch):
    from orders import slot_ladder
    cell = {"n": 100, "ttp_n": 30, "ttp_min": 4.0, "gain_n": 30, "typical_gain_pct": 2.0}
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": cell})
    o = hook.learned_numbers(None, _pos(), 3.0)
    assert o["ttp_min"] == 12.0 and o["typical_gain_pct"] == 2.5          # 0.5 x 4 + 0.5 x 20 = 12, above the 8 minute floor
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": {**cell, "ttp_n": 270, "ttp_min": 1.0}})
    assert hook.learned_numbers(None, _pos(), 3.0)["ttp_min"] == 8.0      # 0.9 x 1 + 0.1 x 20 = 2.9 -> floor 8
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": {**cell, "ttp_n": 270, "ttp_min": 200.0}})
    assert hook.learned_numbers(None, _pos(), 3.0)["ttp_min"] == 60.0     # ceiling


def test_hook_falls_back_to_fixed_numbers(hook, monkeypatch):
    import config
    from orders import slot_ladder
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {})
    o = hook.learned_numbers(None, _pos(), 3.0)
    assert o["ttp_min"] == 20.0 and o["typical_gain_pct"] is None          # nothing learned: signal 4 stays wall-only
    assert hook.learned_numbers(None, _pos(stamp=False), 3.0)["ttp_source"] == "fixed"
    monkeypatch.setattr(config, "MOMENTUM_LEARNED_ENABLED", False, raising=False)
    monkeypatch.setattr(slot_ladder, "cells", lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hook.learned_numbers(None, _pos(), 3.0)["ttp_source"] == "fixed"
    monkeypatch.setattr(config, "MOMENTUM_LEARNED_ENABLED", True, raising=False)
    assert hook.learned_numbers(None, _pos(), 3.0)["ttp_source"] == "fixed"  # a failing read is the fixed numbers, never an exception
