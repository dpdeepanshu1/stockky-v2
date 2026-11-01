"""E283: a long target learned from the setup's own winners (orders/long_target.py). Fixed numbers, no market data, no database."""
import json
import re
import types
from pathlib import Path

import config
from orders import learned_limit, long_target as lt, setup_stats, slot_ladder

ROOT = Path(__file__).resolve().parent.parent
CELL = {"n": 40, "gain_n": 20, "typical_gain_pct": 2.0, "ttp_n": 0}      # learned 2.0 %, w = 20 / 50 = 0.4 -> 0.4 x 2.0 + 0.6 x plan
S1 = {"id": "S1_CATALYST_UP", "slot": "0930"}
KEY = "S1_CATALYST_UP|0930"


def base(monkeypatch, cell=CELL, key=KEY):
    monkeypatch.setattr(config, "LONG_TARGET_LEARNED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LONG_TARGET_MIN_PCT", 0.8, raising=False)
    monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 2.0, raising=False)
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", False, raising=False)
    learned_limit.reset()
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {key: cell} if cell is not None else {})


def levels(live=100.0, stop_pct=0.5, target_pct=4.0):
    return types.SimpleNamespace(stop_price=round(live * (1 - stop_pct / 100), 2), stop_pct=stop_pct,
                                 target_price=round(live * (1 + target_pct / 100), 2), target_pct=target_pct)


def test_learned_target_is_nearer_and_rounded_up_to_the_tick(monkeypatch):
    base(monkeypatch)
    lv = levels()
    note = lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05)
    # 0.4 x 2.0 + 0.6 x 4.0 = 3.2 %  -> 103.20
    assert lv.target_price == 103.2 and abs(lv.target_pct - 3.2) < 1e-6
    assert note["was_target_pct"] == 4.0 and note["cell"] == KEY and note["source"].startswith("learned")


def test_never_farther_than_the_plan_target(monkeypatch):
    base(monkeypatch, dict(CELL, typical_gain_pct=9.0))
    lv = levels(target_pct=3.0)
    assert lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05) == {} and lv.target_price == 103.0


def test_rounding_never_nearer_than_computed(monkeypatch):
    base(monkeypatch, dict(CELL, typical_gain_pct=1.0))
    lv = levels(live=237.37, target_pct=4.0, stop_pct=0.5)
    lt.adjust(lv, 237.37, setup=S1, db=object(), tick=0.05)
    raw = 237.37 * (1 + (0.4 * 1.0 + 0.6 * 4.0) / 100)
    assert lv.target_price >= raw - 1e-9 and lv.target_price - raw < 0.05 + 1e-9


def test_reward_risk_floor_keeps_the_plan_target(monkeypatch):
    base(monkeypatch)
    lv = levels(stop_pct=1.8)                                  # 3.2 % target vs 2 x 1.8 = 3.6 % needed -> plan target stays
    note = lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05)
    assert lv.target_pct == 4.0 and lv.target_price == 104.0 and note["kept_plan"] == "RR_FLOOR" and "target_pct" not in note


def test_floor_is_off_when_min_rr_is_zero(monkeypatch):
    base(monkeypatch)
    monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 0.0, raising=False)
    lv = levels(stop_pct=1.8)
    assert "target_pct" in lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05)


def test_too_few_trades_or_winners_changes_nothing(monkeypatch):
    for c in (dict(CELL, n=29), dict(CELL, gain_n=9), dict(CELL, typical_gain_pct=None)):
        base(monkeypatch, c)
        lv = levels()
        assert lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05) == {} and lv.target_pct == 4.0


def test_legacy_unknown_and_short_setups_never_learn(monkeypatch):
    base(monkeypatch, CELL, "LEGACY_SHORTLIST|0930")
    for st in ({"id": "LEGACY_SHORTLIST", "slot": "0930"}, {"id": "S4_CATALYST_DOWN", "slot": "0930"}, None, "x"):
        lv = levels()
        assert lt.adjust(lv, 100.0, setup=st, db=object(), tick=0.05) == {} and lv.target_pct == 4.0


def test_falls_back_to_the_all_slot_cell(monkeypatch):
    base(monkeypatch, CELL, "S1_CATALYST_UP|ALL")
    lv = levels()
    note = lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05)
    assert note["cell"] == "S1_CATALYST_UP|ALL" and lv.target_pct < 4.0


def test_minimum_pct_ignores_a_tiny_learned_target(monkeypatch):
    base(monkeypatch, dict(CELL, typical_gain_pct=0.1, n=400, gain_n=400))
    monkeypatch.setattr(config, "LONG_TARGET_MIN_PCT", 3.9, raising=False)
    lv = levels()
    assert lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05) == {} and lv.target_pct == 4.0


def test_switch_off_and_no_db_and_bad_levels(monkeypatch):
    base(monkeypatch)
    monkeypatch.setattr(config, "LONG_TARGET_LEARNED_ENABLED", False, raising=False)
    lv = levels()
    assert lt.adjust(lv, 100.0, setup=S1, db=object(), tick=0.05) == {}
    monkeypatch.setattr(config, "LONG_TARGET_LEARNED_ENABLED", True, raising=False)
    assert lt.adjust(lv, 100.0, setup=S1, db=None, tick=0.05) == {}
    assert lt.adjust(types.SimpleNamespace(), 100.0, setup=S1, db=object()) == {} and lt.adjust(lv, None, setup=S1, db=object()) == {}
    bad = types.SimpleNamespace(stop_price=99.5, stop_pct=0.5, target_price=99.0, target_pct=4.0)      # target below the price: not ours to fix
    assert lt.adjust(bad, 100.0, setup=S1, db=object()) == {}


def test_weekly_limit_applies(monkeypatch):
    base(monkeypatch, dict(CELL, n=400, gain_n=400, typical_gain_pct=1.0))
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", True, raising=False)
    learned_limit.reset()
    g = lt.learned_gain(S1, None, object(), 4.0)
    assert g and g["gain_pct"] > 0


def test_stamp(monkeypatch):
    assert lt.stamp("{}", {}) == "{}"
    out = json.loads(lt.stamp(json.dumps({"x": 1}), {"target_pct": 3.2}))
    assert out["x"] == 1 and out["long_target"]["target_pct"] == 3.2
    assert lt.stamp(None, None) is None


def test_wiring_is_read_as_text_and_switches_are_registered():
    ent = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    i_ls, i_lt, i_tm = ent.index("learned_stop.adjust("), ent.index("long_target.adjust("), ent.index("tm_gate.entry_check(candidate.symbol, \"LONG\"")
    assert i_ls < i_lt < i_tm                                  # the stop first, then the target, then the Time Machine judges both
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    for k in ("LONG_TARGET_LEARNED_ENABLED", "LONG_TARGET_MIN_PCT"):
        assert f'Flag("{k}"' in cat and f'"{k}":' in auto and re.search(rf"^{k} = ", cfg, re.M)
