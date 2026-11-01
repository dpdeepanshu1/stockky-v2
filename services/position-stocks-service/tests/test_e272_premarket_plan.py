"""E272: premarket_plan + adaptive_params. Plain runner: python3 tests/test_e272_premarket_plan.py"""
from __future__ import annotations
import importlib.util, os, sys
from datetime import date
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
def load(n, rel):
    sp = importlib.util.spec_from_file_location(n, os.path.join(ROOT, rel)); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
ap = load("ap", "orders/adaptive_params.py")
from screening import premarket_plan as pm  # noqa: E402

def test_zscore_percentile():
    h = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert ap.zscore(5.5, h) == 0.0
    assert ap.zscore(5, [1, 2]) is None and ap.zscore(5, [3] * 10) is None
    assert ap.percentile_of(10, h) == 95.0 and ap.percentile_of(1, [1] * 3 + [2] * 5) is not None
    assert abs(ap.percentile_value(h, 95) - 9.55) < 1e-9 and ap.percentile_value(h[:3], 50) is None

def test_shrink():
    assert ap.shrink(10, 4, 0) == 4
    assert abs(ap.shrink(10, 4, 30) - 7.0) < 1e-9
    assert abs(ap.shrink(10, 4, 15) - (15 / 45 * 10 + 30 / 45 * 4)) < 1e-9
    assert abs(ap.shrink(10, 4, 100) - (100 / 130 * 10 + 30 / 130 * 4)) < 1e-9
    assert ap.shrink(None, 4, 50) == 4

def test_weekly_limit_and_learned():
    assert ap.limit_weekly_change(20, 10) == 12.5 and ap.limit_weekly_change(1, 10) == 7.5 and ap.limit_weekly_change(11, 10) == 11
    assert ap.limit_weekly_change(20, None) == 20
    assert ap.learned_value(10, 4, 29)["source"] == "constant" and ap.learned_value(10, 4, 29)["value"] == 4
    r = ap.learned_value(10, 4, 30); assert r["source"] == "learned" and abs(r["value"] - 7.0) < 1e-9
    r = ap.learned_value(10, 4, 100, previous=4); assert r["value"] <= 5.0 + 1e-9
    r = ap.learned_value(50, 4, 1000, ceiling=5); assert r["value"] == 5 and r["source"] == "ceiling"

def body(bd, **chg):
    return {"rows": {k: {"day_change_pct": v, "bar_date": bd} for k, v in chg.items()}}

def test_cues_freshness_and_verdict():
    today = date(2026, 11, 2)                      # Monday: last trading day is Friday 30 Oct
    c = pm.cue_rows(body("2026-10-30", **{"ES=F": -1.81, "NQ=F": -3.48, "^N225": -2.12, "^GDAXI": -0.85, "^FTSE": 0.07, "^HSI": 1.46}), today)
    assert len(c["cues"]) == 6 and c["bar_date"] == "2026-10-30"
    v = pm.cue_verdict(c["cues"]); assert v["risk_off"] is True and v["avg_pct"] < -0.9
    old = pm.cue_rows(body("2026-10-28", **{"ES=F": -2.0}), today); assert old["cues"] == {} and old["stale"] == ["ES=F"]
    nodate = pm.cue_rows({"rows": {"ES=F": {"day_change_pct": 1.0}}}, today); assert nodate["cues"] == {}
    assert pm.cue_verdict({"ES=F": -5.0})["risk_off"] is None
    assert pm.cue_verdict({"ES=F": 0.5, "NQ=F": 0.4, "^N225": 0.1})["risk_off"] is False

def test_vix_events_sectors():
    assert pm.vix_regime(27, list(range(10, 30)))["label"] == "STRESSED" and pm.vix_regime(11, list(range(10, 30)))["label"] == "CALM"
    assert pm.vix_regime(15, [1, 2])["label"] == "UNKNOWN"
    b = {"ok": True, "events": [{"symbol": "AAA", "type": "results", "polarity": 1, "strength": 0.8, "age_s": 600, "published": "x"},
                                {"symbol": "AAA", "type": "order win", "polarity": 1, "strength": 0.5},
                                {"symbol": "BBB", "type": "probe", "polarity": -1, "strength": 0.6},
                                {"symbol": "CCC", "type": "other", "polarity": 0, "strength": 0.9}]}
    e = pm.event_records(b); assert set(e) == {"AAA", "BBB"} and e["AAA"]["result_out"] and e["AAA"]["age_min"] == 10.0 and not e["BBB"]["result_out"]
    assert pm.event_records({"ok": False, "events": b["events"]}) == {} and pm.event_records(b, ["BBB"]).keys() == {"BBB"}
    sec = {"a": "X", "b": "X", "c": "X", "d": "Y", "e": "Y", "f": "Y", "g": "Z", "h": "Z", "i": "Z"}
    gaps = {"a": 2, "b": 2, "c": 2, "d": 0, "e": 0, "f": 0, "g": -2, "h": -2, "i": -2}
    s = pm.sector_strength(gaps, sec.get); assert s["X"]["third"] == "top" and s["Z"]["third"] == "bottom" and s["Y"]["third"] == "mid" and s["X"]["rank"] == 1

def test_plan_and_cadence():
    p = pm.build_plan(today=date(2026, 11, 2), cues_body=None, events_body=None)
    assert p["cues"] == {} and p["events"] == {} and p["verdict"]["risk_off"] is None
    from datetime import datetime
    pm.reset()
    from tz_utils import IST
    def at(h, m): return datetime(2026, 11, 2, h, m, tzinfo=IST)
    assert not pm.due(at(8, 29)) and pm.due(at(8, 30))
    pm._plan["builds"] = 1; pm._plan["last_minute"] = 8 * 60 + 30
    assert not pm.due(at(8, 45)) and pm.due(at(9, 5)) and pm.due(at(9, 6)) and pm.due(at(9, 20))

def test_sector_third_fallback():
    from datetime import datetime
    from tz_utils import IST, ist_today_str
    pm.reset()
    assert pm.sector_third("AAA") is None
    pm._plan.update({"day": ist_today_str(), "plan": {"sectors": {"Banks": {"third": "top"}}}})
    import feed.sector_map as sm
    orig = sm.sector_of; sm.sector_of = lambda s: "Banks"
    try:
        assert pm.sector_third("AAA") == "top"
    finally:
        sm.sector_of = orig; pm.reset()


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("ok", n)
