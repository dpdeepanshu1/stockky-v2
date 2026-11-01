"""E282 round 76: a stop learned from the setup's own winners (orders/learned_stop.py + setup_stats adverse excursion). Fixed numbers, no market data, no database."""
import json
import re
import sys
import types
from pathlib import Path

import pytest

import config
from orders import learned_limit, learned_stop as ls, setup_stats, slot_ladder

ROOT = Path(__file__).resolve().parent.parent

try:
    from orders import adaptive as _adaptive          # the real module (needs httpx / sqlalchemy, present in the service image)
except ImportError:                                    # a bare sandbox: the same E251 formula as orders/adaptive.stop_floor_pct, so the floor arithmetic below is still exercised
    _adaptive = types.ModuleType("orders.adaptive")

    def _stop_floor_pct(daily_atr_pct, m1_range_pct):
        parts = []
        frac, mult = float(config.ADAPTIVE_STOP_DAILY_ATR_FRAC), float(config.ADAPTIVE_STOP_M1_MULT)
        if daily_atr_pct and frac > 0:
            parts.append(frac * float(daily_atr_pct))
        if m1_range_pct and mult > 0:
            parts.append(mult * float(m1_range_pct))
        if not parts:
            return 0.0, ""
        val = max(parts)
        cap = float(config.ADAPTIVE_STOP_FLOOR_MAX_PCT)
        return (cap if cap > 0 and val > cap else val), ""
    _adaptive.stop_floor_pct = _stop_floor_pct
    sys.modules["orders.adaptive"] = _adaptive
    import orders
    orders.adaptive = _adaptive
CELL = {"n": 40, "mae_n": 20, "adverse_p90_pct": 0.8}          # learned 0.80 x 1.10 = 0.88 %, w = 20 / 50 = 0.4 -> 0.4 x 0.88 + 0.6 x plan
S1 = {"id": "S1_CATALYST_UP", "slot": "0930"}
KEY = "S1_CATALYST_UP|0930"


@pytest.fixture(autouse=True)
def base(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_STOP_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LEARNED_STOP_MIN_PCT", 0.5, raising=False)
    monkeypatch.setattr(config, "ADAPTIVE_STOP_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3, raising=False)
    monkeypatch.setattr(config, "ADAPTIVE_STOP_DAILY_ATR_FRAC", 0.45, raising=False)
    monkeypatch.setattr(config, "ADAPTIVE_STOP_M1_MULT", 2.0, raising=False)
    monkeypatch.setattr(config, "ADAPTIVE_STOP_FLOOR_MAX_PCT", 1.5, raising=False)
    learned_limit.reset()


def cells(monkeypatch, c):
    monkeypatch.setattr(slot_ladder, "cells", lambda db: c)


def long_levels(live=100.0, stop_pct=1.5):
    return types.SimpleNamespace(stop_price=round(live * (1 - stop_pct / 100), 2), stop_pct=stop_pct, target_price=round(live * 1.04, 2), target_pct=4.0)


def short_levels(live=100.0, stop_pct=1.5):
    return types.SimpleNamespace(stop_price=round(live * (1 + stop_pct / 100), 2), stop_pct=stop_pct, target_price=round(live * 0.97, 2), target_pct=3.0)


def go(levels, setup=S1, side="LONG", atr=1.0, **kw):
    return ls.adjust(levels, 100.0, symbol="X", setup=setup, now=None, db=object(), side=side, atr_pct=atr, m1_pct=None, tick=0.05, **kw)


# ---- setup_stats: how far a WINNER went against its entry ----------------------------------------------------------------------------------------------------------------

def _row(pnl, entry, lo, hi, slot="0930", side="LONG", sid="S1_CATALYST_UP"):
    fj = json.dumps({"setup": {"id": sid, "slot": slot, "side": side, "trigger": "x", "confirmations": []}})
    return {"flow_json": fj, "realized_pnl": pnl, "entry_price": entry, "stop_price": entry * 0.985, "quantity": 10, "opened_at": "2026-10-20 09:30:00",
            "min_price_seen": lo, "max_price_seen": hi, "side": side}


def test_adverse_excursion_is_read_from_winners_only():
    rows = [_row(50.0, 100.0, 99.0 - 0.1 * i, 103.0) for i in range(12)]          # 12 winners: lows 99.0 ... 97.9 -> 1.0 ... 2.1 % against
    rows += [_row(-30.0, 100.0, 95.0, 100.5) for _ in range(20)]                    # 20 losers dipped 5 %: must not count
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["n"] == 32 and c["mae_n"] == 12
    assert 1.9 < c["adverse_p90_pct"] < 2.1


def test_adverse_excursion_needs_ten_winners():
    rows = [_row(50.0, 100.0, 99.0, 103.0) for _ in range(9)]
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["mae_n"] == 9 and c["adverse_p90_pct"] is None


def test_a_winner_that_never_went_against_the_entry_counts_as_zero():
    rows = [_row(50.0, 100.0, 100.5, 103.0) for _ in range(10)]
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["mae_n"] == 10 and c["adverse_p90_pct"] == 0.0


def test_a_short_reads_the_highest_price_as_its_adverse_move():
    rows = [_row(50.0, 100.0, 97.0, 100.6, side="SHORT", sid="S4_CATALYST_DOWN") for _ in range(10)]
    c = setup_stats.summarize(rows)["S4_CATALYST_DOWN|0930"]
    assert c["adverse_p90_pct"] == 0.6


def test_unreadable_prices_are_skipped_not_zero():
    rows = [_row(50.0, 100.0, None, 103.0) for _ in range(10)]
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|0930"]
    assert c["mae_n"] == 0 and c["adverse_p90_pct"] is None


# ---- which cell speaks -------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_cell_with_too_few_trades_gives_nothing(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, n=29)})
    assert ls.cell_adverse(S1, None, object(), "LONG") is None


def test_a_cell_with_too_few_winners_gives_nothing(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, mae_n=9)})
    assert ls.cell_adverse(S1, None, object(), "LONG") is None


def test_the_slot_cell_is_used_then_the_all_cell(monkeypatch):
    cells(monkeypatch, {"S1_CATALYST_UP|ALL": dict(CELL, adverse_p90_pct=0.7)})
    assert ls.cell_adverse(S1, None, object(), "LONG")["cell"] == "S1_CATALYST_UP|ALL"
    cells(monkeypatch, {KEY: dict(CELL), "S1_CATALYST_UP|ALL": dict(CELL, adverse_p90_pct=0.7)})
    assert ls.cell_adverse(S1, None, object(), "LONG")["adverse_pct"] == 0.8


def test_the_legacy_shortlist_never_learns(monkeypatch):
    cells(monkeypatch, {"LEGACY_SHORTLIST|0930": dict(CELL), "LEGACY_SHORTLIST|ALL": dict(CELL)})
    assert ls.cell_adverse({"id": "LEGACY_SHORTLIST", "slot": "0930"}, None, object(), "LONG") is None
    assert ls.cell_adverse(None, None, object(), "LONG") is None


def test_a_short_s2_reads_its_own_short_cell_and_a_long_never_reads_s4(monkeypatch):
    cells(monkeypatch, {"S2_RS_BREAKOUT_SHORT|0930": dict(CELL), "S2_RS_BREAKOUT|0930": dict(CELL, adverse_p90_pct=0.3)})
    got = ls.cell_adverse({"id": "S2_RS_BREAKOUT", "slot": "0930"}, None, object(), "SHORT")
    assert got["cell"] == "S2_RS_BREAKOUT_SHORT|0930" and got["adverse_pct"] == 0.8
    assert ls.cell_adverse({"id": "S4_CATALYST_DOWN", "slot": "0930"}, None, object(), "LONG") is None
    assert ls.cell_adverse(S1, None, object(), "SHORT") is None                       # S1 is long only


# ---- the value ----------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_value_is_shrunk_toward_the_plan_stop():
    v = ls.learned_stop_pct(1.5, {"adverse_pct": 0.8, "n": 20, "cell": "c"})
    assert abs(v - (0.4 * 0.88 + 0.6 * 1.5)) < 1e-9
    v30 = ls.learned_stop_pct(1.5, {"adverse_pct": 0.8, "n": 30, "cell": "c"})        # w = 0.5
    assert abs(v30 - (0.5 * 0.88 + 0.5 * 1.5)) < 1e-9
    assert ls.learned_stop_pct(1.5, {"adverse_pct": 0.0, "n": 30, "cell": "c"}) is None
    assert ls.learned_stop_pct(0.0, {"adverse_pct": 0.8, "n": 30, "cell": "c"}) is None


def test_the_weekly_limit_holds_a_jump(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_PCT", 25.0, raising=False)
    a = {"adverse_pct": 2.0, "n": 1000, "cell": "k"}
    first = ls.learned_stop_pct(3.0, a, now_ts=1.8e9)                                  # first sight: anchor = 2.2 %
    jumped = ls.learned_stop_pct(3.0, dict(a, adverse_pct=0.4), now_ts=1.8e9 + 86400)  # winners suddenly look calm: at most 25 % lower than the anchor
    assert jumped > 0.75 * first - 0.2


# ---- adjust: long ------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_long_stop_is_tightened_and_rounded_down_to_the_tick(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    lv = long_levels()
    note = go(lv)
    assert lv.stop_price == 98.7 and lv.stop_pct == 1.3                                # plan 1.5 %: 0.4 x 0.88 + 0.6 x 1.5 = 1.252 % -> 98.748 -> 98.70
    assert lv.target_price == 104.0 and lv.target_pct == 4.0                           # the target is not touched
    assert note["was_stop_pct"] == 1.5 and note["stop_pct"] == 1.3 and note["cell"] == KEY and note["n"] == 20 and note["side"] == "LONG"
    assert note["stop_pct"] >= 1.252                                                   # rounding is never tighter than computed


def test_the_stop_never_gets_wider(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, adverse_p90_pct=3.0)})                         # winners endured 3 %: more than the plan stop
    lv = long_levels()
    assert go(lv) == {} and lv.stop_price == 98.5 and lv.stop_pct == 1.5


def test_a_tiny_step_is_not_a_change(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, adverse_p90_pct=1.3)})                         # 0.4 x 1.43 + 0.6 x 1.5 = 1.472 -> only 0.028 tighter... after tick rounding below the step
    lv = long_levels(stop_pct=1.49)
    assert go(lv) == {} and lv.stop_pct == 1.49


def test_the_noise_floor_wins_over_a_learned_value(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    lv = long_levels()
    note = go(lv, atr=3.0)                                                             # floor = max(0.5, 0.3 x 3, 0.45 x 3 capped 1.5) = 1.35 > 1.252
    assert lv.stop_price == 98.65 and lv.stop_pct == 1.35 and note["floor_pct"] == 1.35


def test_the_configured_minimum_wins(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL, adverse_p90_pct=0.1)})
    monkeypatch.setattr(config, "LEARNED_STOP_MIN_PCT", 1.2, raising=False)
    lv = long_levels()
    go(lv)
    assert lv.stop_pct >= 1.2


def test_an_unreadable_noise_floor_leaves_the_stop_alone(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    def boom(*a, **k):
        raise RuntimeError("no volatility data")
    monkeypatch.setattr(_adaptive, "stop_floor_pct", boom)
    lv = long_levels()
    assert go(lv) == {} and lv.stop_price == 98.5                                      # fail closed: the E251 floor is on and cannot be read
    monkeypatch.setattr(config, "ADAPTIVE_STOP_FLOOR_ENABLED", False, raising=False)
    assert go(lv)["stop_pct"] == 1.3                                                   # with that floor switched off the other two floors still apply


def test_an_unknown_daily_atr_leaves_the_stop_alone(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    monkeypatch.setattr(ls, "floor_pct", lambda symbol, atr=None, m1=None: None)
    lv = long_levels()
    assert go(lv) == {} and lv.stop_price == 98.5


def test_switch_off_and_missing_cell_change_nothing(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    monkeypatch.setattr(config, "LEARNED_STOP_ENABLED", False, raising=False)
    lv = long_levels()
    assert go(lv) == {} and lv.stop_price == 98.5
    monkeypatch.setattr(config, "LEARNED_STOP_ENABLED", True, raising=False)
    cells(monkeypatch, {})
    assert go(lv) == {} and lv.stop_price == 98.5
    assert ls.adjust(lv, 100.0, symbol="X", setup=S1, now=None, db=None, atr_pct=1.0) == {}      # no database handle


def test_a_stop_on_the_wrong_side_of_price_is_left_alone(monkeypatch):
    cells(monkeypatch, {KEY: dict(CELL)})
    lv = long_levels()
    lv.stop_price = 101.0
    assert go(lv) == {} and lv.stop_price == 101.0
    assert ls.adjust(types.SimpleNamespace(), 100.0, symbol="X", setup=S1, now=None, db=object(), atr_pct=1.0) == {}
    assert ls.adjust(lv, "junk", symbol="X", setup=S1, now=None, db=object(), atr_pct=1.0) == {}


# ---- adjust: short (mirror) ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_short_stop_is_tightened_and_rounded_up_to_the_tick(monkeypatch):
    s4 = {"id": "S4_CATALYST_DOWN", "slot": "0930"}
    cells(monkeypatch, {"S4_CATALYST_DOWN|0930": dict(CELL)})
    lv = short_levels()
    note = go(lv, setup=s4, side="SHORT")
    assert lv.stop_price == 101.3 and lv.stop_pct == 1.3 and lv.target_price == 97.0
    assert note["side"] == "SHORT" and note["stop_pct"] >= 1.252


def test_a_short_stop_never_gets_wider(monkeypatch):
    s4 = {"id": "S4_CATALYST_DOWN", "slot": "0930"}
    cells(monkeypatch, {"S4_CATALYST_DOWN|0930": dict(CELL, adverse_p90_pct=3.0)})
    lv = short_levels()
    assert go(lv, setup=s4, side="SHORT") == {} and lv.stop_price == 101.5


def test_a_short_never_uses_a_long_cell(monkeypatch):
    cells(monkeypatch, {"S2_RS_BREAKOUT|0930": dict(CELL)})                            # only the LONG S2 cell is learned
    lv = short_levels()
    assert go(lv, setup={"id": "S2_RS_BREAKOUT", "slot": "0930"}, side="SHORT") == {} and lv.stop_price == 101.5


# ---- stamp -------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_stamp_adds_the_note_and_leaves_everything_else():
    out = json.loads(ls.stamp(json.dumps({"setup": {"id": "S1_CATALYST_UP"}}), {"stop_pct": 1.3}))
    assert out["learned_stop"] == {"stop_pct": 1.3} and out["setup"]["id"] == "S1_CATALYST_UP"
    assert ls.stamp("not json", {"a": 1}) == "not json"
    assert ls.stamp('{"a":1}', {}) == '{"a":1}'
    assert ls.stamp('{"a":1}', None) == '{"a":1}'


# ---- wiring (read as text, so it runs without a database driver) --------------------------------------------------------------------------------------------------

def test_the_long_entry_sizes_from_the_plan_stop_and_stamps_the_note():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert "learned_stop.adjust(" in src and "_budget_stop_pct = levels.stop_pct" in src
    assert "adaptive_stop_pct=_budget_stop_pct, conviction_mult=" in src
    assert src.index("learned_stop.adjust(") < src.index("tm_gate.entry_check(candidate.symbol, \"LONG\"")      # the Time Machine judges the NEW stop
    assert src.index("learned_stop.adjust(") > src.index("setup_live.no_setup_refusal(")                    # the setup is known


def test_the_short_plan_keeps_the_plan_stop_for_size_and_hands_the_note_to_the_stamp():
    src = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert src.index("budget_stop_pct = float(levels.stop_pct)") < src.index("learned_stop.adjust(")
    assert '"stop_note": _snote' in src
    live = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")
    assert 'plan.get("stop_note")' in live


def test_the_switches_are_on_the_calibration_page_and_protected_from_the_calibrator():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    for k in ("LEARNED_STOP_ENABLED", "LEARNED_STOP_MIN_PCT"):
        assert f'Flag("{k}"' in cat and f'"{k}":' in auto
        assert re.search(rf"^{k} = ", (ROOT / "config.py").read_text(encoding="utf-8"), re.M)
