"""E285 round 107: the trail distance learned from the setup winners' recovered dips (excursion record, setup_stats, trail_dip, exit_adaptive, trailing wiring).
Plain-function runner: python3 tests/test_e285_round107_trail_dip.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import exit_adaptive, setup_stamp, setup_stats, trail_dip

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_excursion():
    """orders/excursion.py needs sqlalchemy + models at import; stub them so the pure functions run anywhere."""
    import importlib.util
    saved = {k: sys.modules.get(k) for k in ("sqlalchemy", "sqlalchemy.orm", "models", "tz_utils")}
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.Session = object
    models = types.ModuleType("models"); models.ScalpPosition = object
    tz = types.ModuleType("tz_utils"); tz.as_aware = lambda x: x
    sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm, "models": models, "tz_utils": tz})
    try:
        spec = importlib.util.spec_from_file_location("excursion_r107", os.path.join(_HERE, "..", "orders", "excursion.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        return m
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


exc = _load_excursion()


def _t(prices):
    return [(i, p) for i, p in enumerate(prices)]


# ── recovered_dip_pct ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_long_dip_is_counted_only_after_a_new_high():
    # 100 -> 102 (profit peak) -> 101 (dip 0.98 %) -> 103 (new high: the dip was recovered) -> 102.5 (still going, not counted)
    d = exc.recovered_dip_pct(_t([100, 101, 102, 101, 103, 102.5]), 100.0)
    assert d == round((102 - 101) / 102 * 100, 4)


def test_a_pullback_that_never_recovers_is_not_counted():
    assert exc.recovered_dip_pct(_t([100, 102, 100.5, 100.2]), 100.0) == 0.0          # in profit, but the only dip ended the trade


def test_never_in_profit_is_none_and_dips_below_entry_before_profit_do_not_count():
    assert exc.recovered_dip_pct(_t([100, 99, 98, 99.5]), 100.0) is None
    d = exc.recovered_dip_pct(_t([100, 98, 101, 100.5, 101.5]), 100.0)                # the 98 is adverse excursion, not a giveback
    assert d == round((101 - 100.5) / 101 * 100, 4)


def test_deepest_of_several_recovered_dips_wins():
    d = exc.recovered_dip_pct(_t([100, 102, 101.5, 103, 101, 104]), 100.0)
    assert d == round((103 - 101) / 103 * 100, 4)


def test_short_mirror():
    # entry 100, falls to 98 (profit trough), bounces to 99 (dip 1.02 % of 98), makes a new low 97
    d = exc.recovered_dip_pct(_t([100, 99, 98, 99, 97]), 100.0, short=True)
    assert d == round((99 - 98) / 98 * 100, 4)
    assert exc.recovered_dip_pct(_t([100, 101, 102]), 100.0, short=True) is None


def test_bad_input_never_raises():
    assert exc.recovered_dip_pct([("x", "y")], 100.0) is None
    assert exc.recovered_dip_pct(None, 100.0) is None
    assert exc.recovered_dip_pct(_t([100, 101]), 0) is None


def test_stamp_dip_only_raises_and_keeps_other_keys():
    fj = json.dumps({"peak": {"hi_at": 5.0}, "setup": {"id": "S1"}})
    out = exc.stamp_dip(fj, 0.8)
    d = json.loads(out)
    assert d["peak"] == {"hi_at": 5.0, "dip_pct": 0.8} and d["setup"] == {"id": "S1"}
    assert exc.stamp_dip(out, 0.5) == out                                              # never lowered
    assert json.loads(exc.stamp_dip(out, 1.2))["peak"]["dip_pct"] == 1.2
    assert exc.stamp_dip("{bad", 0.5) == "{bad"
    assert exc._stored_dip(out) == 0.8 and exc._stored_dip(None) is None


# ── setup_stats ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _row(i, dip, pnl=100.0, sid="S1_CATALYST_UP"):
    st = setup_stamp.build(setup_id=sid)
    fj = {"setup": st}
    if dip is not None:
        fj["peak"] = {"dip_pct": dip}
    return {"flow_json": json.dumps(fj), "realized_pnl": pnl, "entry_price": 100.0, "stop_price": 99.0, "quantity": 100, "opened_at": f"2026-11-{2 + i % 6:02d} 09:40:00"}


def test_winners_dips_give_a_p75_with_ten_samples_only():
    rows = [_row(i, 0.2 + 0.1 * i) for i in range(12)]
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|ALL"]
    assert c["dip_n"] == 12 and c["dip_p75_pct"] is not None and 0.2 <= c["dip_p75_pct"] <= 1.4
    c9 = setup_stats.summarize(rows[:9])["S1_CATALYST_UP|ALL"]
    assert c9["dip_n"] == 9 and c9["dip_p75_pct"] is None


def test_losers_and_unrecorded_trades_are_not_samples():
    rows = [_row(i, 0.5, pnl=-50.0) for i in range(5)] + [_row(i, None) for i in range(5)]
    c = setup_stats.summarize(rows)["S1_CATALYST_UP|ALL"]
    assert c["dip_n"] == 0 and c["dip_p75_pct"] is None


def test_a_zero_dip_winner_counts():
    c = setup_stats.summarize([_row(i, 0.0) for i in range(10)])["S1_CATALYST_UP|ALL"]
    assert c["dip_n"] == 10 and c["dip_p75_pct"] == 0.0


# ── trail_dip ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_widen_adds_a_share_of_the_extra_and_never_narrows():
    d = {"dip_pct": 1.0, "n": 30, "cell": "t|x"}            # w = 30 / 60 = 0.5
    assert abs(trail_dip.widen(0.6, d) - (0.6 + 0.5 * 0.4)) < 1e-9
    assert trail_dip.widen(1.5, d) == 1.5                    # learned is not wider: unchanged
    assert trail_dip.widen(0.6, None) == 0.6
    assert trail_dip.widen(0.6, {"dip_pct": "x", "n": 5}) == 0.6
    assert trail_dip.widen(0.6, {"dip_pct": 1.0, "n": 0}) == 0.6


def test_cell_dip_needs_30_trades_10_dips_and_an_explicit_setup():
    from orders import slot_ladder
    cells = {"S1_CATALYST_UP|0930": {"n": 40, "dip_n": 12, "dip_p75_pct": 0.9}, "S1_CATALYST_UP|ALL": {"n": 50, "dip_n": 20, "dip_p75_pct": 0.7},
             "LEGACY_SHORTLIST|ALL": {"n": 90, "dip_n": 40, "dip_p75_pct": 0.5}, "S1_CATALYST_UP|1000": {"n": 29, "dip_n": 15, "dip_p75_pct": 2.0}}
    old = slot_ladder.cells
    slot_ladder.cells = lambda db: cells
    try:
        def pos(sid, slot, side="LONG"):
            return types.SimpleNamespace(flow_json=json.dumps({"setup": setup_stamp.build(setup_id=sid, side=side) | {"slot": slot}}))
        r = trail_dip.cell_dip(pos("S1_CATALYST_UP", "0930"), object())
        assert r == {"dip_pct": 0.9, "n": 12, "cell": "S1_CATALYST_UP|0930"}
        r = trail_dip.cell_dip(pos("S1_CATALYST_UP", "1000"), object())               # slot cell under 30 trades -> the ALL cell
        assert r["cell"] == "S1_CATALYST_UP|ALL" and r["dip_pct"] == 0.7
        assert trail_dip.cell_dip(pos(setup_stamp.LEGACY_ID, "0930"), object()) is None
        assert trail_dip.cell_dip(pos("S4_CATALYST_DOWN", "0930", "LONG"), object()) is None      # a short-only setup on a long stamp
        assert trail_dip.cell_dip(types.SimpleNamespace(flow_json=None), object()) is None
        assert trail_dip.cell_dip(pos("S1_CATALYST_UP", "0930"), None) is None
    finally:
        slot_ladder.cells = old


def test_switch_off_gives_none_and_no_widening():
    config.TRAIL_LEARNED_DIP_ENABLED = False
    try:
        assert trail_dip.cell_dip(types.SimpleNamespace(flow_json="{}"), object()) is None
        assert trail_dip.widen(0.6, {"dip_pct": 2.0, "n": 100, "cell": "a"}) == 0.6
    finally:
        config.TRAIL_LEARNED_DIP_ENABLED = True


# ── exit_adaptive ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_trail_distance_unchanged_without_a_dip():
    for sp, br in ((1.0, None), (1.0, 0.3), (1.2, 0.9), (0.0, 0.5), (2.0, 3.0)):
        assert exit_adaptive.trail_distance_pct(sp, br) == exit_adaptive.trail_distance_pct(sp, br, None)


def test_dip_widens_the_trail_but_never_past_the_stop():
    base = exit_adaptive.trail_distance_pct(1.0, 0.3)
    wide = exit_adaptive.trail_distance_pct(1.0, 0.3, {"dip_pct": 0.9, "n": 300, "cell": "w|x"})
    assert wide > base and wide <= 1.0 + 1e-9
    huge = exit_adaptive.trail_distance_pct(1.0, 0.3, {"dip_pct": 5.0, "n": 300, "cell": "h|x"})
    assert huge <= 1.0 + 1e-9                                                        # capped by the stop


def test_adaptive_off_ignores_the_dip():
    config.TRAIL_ADAPTIVE_ENABLED = False
    try:
        assert exit_adaptive.trail_distance_pct(1.0, 0.3, {"dip_pct": 0.9, "n": 300, "cell": "o|x"}) == exit_adaptive.trail_distance_pct(1.0, 0.3)
    finally:
        config.TRAIL_ADAPTIVE_ENABLED = True


def test_trailing_stop_sits_lower_with_a_dip():
    import importlib.util
    names = ("sqlalchemy", "sqlalchemy.orm", "models", "execution", "execution.dhan_client")
    saved = {k: sys.modules.get(k) for k in names}
    sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.Session = object
    models = types.ModuleType("models"); models.ScalpPosition = object
    ex = types.ModuleType("execution"); dc = types.ModuleType("execution.dhan_client")
    dc.tick_size_for_price = lambda p: 0.05
    dc.round_to_tick = lambda x: round(round(x / 0.05) * 0.05, 2)
    ex.dhan_client = dc
    sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm, "models": models, "execution": ex, "execution.dhan_client": dc})
    try:
        spec = importlib.util.spec_from_file_location("trailing_r107", os.path.join(_HERE, "..", "orders", "trailing.py"))
        trailing = importlib.util.module_from_spec(spec); spec.loader.exec_module(trailing)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    kw = dict(entry=100.0, peak=103.0, ltp=102.8, current_stop=98.5, target=106.0, stop_pct=1.5, bar_range_pct=0.2)
    a = trailing.compute_trailing_stop(**kw)
    b = trailing.compute_trailing_stop(**kw, dip={"dip_pct": 1.2, "n": 300, "cell": "t|y"})
    assert a is not None and b is not None and b < a


def test_wiring_by_source_text():
    src = open(os.path.join(_HERE, "..", "orders", "excursion.py"), encoding="utf-8").read()
    assert "recovered_dip_pct(_t, pos.entry_price" in src and "stamp_dip(getattr(pos" in src
    tr = open(os.path.join(_HERE, "..", "orders", "trailing.py"), encoding="utf-8").read()
    assert "dip=_learned_dip(pos, db)" in tr and tr.count("trail_distance_pct(stop_pct, bar_range_pct, dip)") == 2
    cat = open(os.path.join(_HERE, "..", "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("TRAIL_LEARNED_DIP_ENABLED"' in cat


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("ok", k)
    print(n, "passed")
