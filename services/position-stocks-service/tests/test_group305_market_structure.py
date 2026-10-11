"""group305/307: pure market-structure maths."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from screening import market_structure as ms


def bar(i, o, h, l, c, v=1000, t0=1_790_000_000 - 1_790_000_000 % 300):
    return (t0 + i * 300, o, h, l, c, v)


def quiet(n, base=100.0, start=0):
    out = []
    for k in range(n):
        up = k % 2 == 0
        out.append(bar(start + k, base + (0.0 if up else 0.1), base + 0.25, base - 0.15, base + (0.1 if up else 0.0)))
    return out


def bullish_series():
    bars = quiet(10)
    n = len(bars)
    bars.append(bar(n, 100.1, 100.15, 99.6, 99.7))
    bars.append(bar(n + 1, 99.75, 102.6, 99.7, 102.5))
    bars.append(bar(n + 2, 102.5, 103.0, 102.2, 102.8))
    bars.append(bar(n + 3, 102.8, 103.2, 102.5, 103.0))
    return bars


def test_strength_clean_bull_bear_and_choppy():
    s = ms.strength(103, 100, 104, 99.5, 2.0)
    assert s["side"] == "bull" and s["clean"] and s["score"] == pytest.approx(1.5)
    assert ms.strength(97, 100, 100.5, 96, 2.0)["side"] == "bear"
    c = ms.strength(101, 100, 104, 97, 2.0)
    assert not c["clean"] and c["score"] == 0.0
    assert ms.strength(101, 100, 104, 97, 2.0, ratio=1.2)["clean"] is True
    f = ms.strength(100, 100, 101, 99, 2.0)
    assert f["side"] is None and f["score"] == 0.0


@pytest.mark.parametrize("args", [(0, 100, 101, 99, 2.0), (100, 0, 101, 99, 2.0), (100, 100, 99, 101, 2.0),
                                  (100, 100, 101, 99, None), (100, 100, 101, 99, 0), (float("nan"), 100, 101, 99, 2.0)])
def test_strength_none_on_bad_input(args):
    assert ms.strength(*args) is None


def test_rank_orders_percentiles_ties_and_empty():
    rows = [{"symbol": "A", "side": "bull", "score": 1.0}, {"symbol": "B", "side": "bull", "score": 3.0},
            {"symbol": "C", "side": "bull", "score": 2.0}, {"symbol": "D", "side": "bear", "score": 9.0},
            {"symbol": "E", "side": "bull", "score": 0.0}]
    out = ms.rank_rows(rows, "bull")
    assert [r["symbol"] for r in out] == ["B", "C", "A"] and [r["rank"] for r in out] == [1, 2, 3]
    assert out[0]["percentile"] == pytest.approx(83.3, abs=0.05) and out[-1]["percentile"] == pytest.approx(16.7, abs=0.05)
    assert [r["symbol"] for r in ms.rank_rows([{"symbol": "Z", "side": "bull", "score": 1}, {"symbol": "A", "side": "bull", "score": 1}], "bull")] == ["A", "Z"]
    assert ms.rank_rows([], "bull") == []


def test_conviction():
    assert ms.conviction(100, 2.0, 101, 100, "bull", 1.0, 1.0) == 100
    assert 0 < ms.conviction(0, None, 100, None, "bull", None, None) < 50
    assert ms.conviction(80, 1.0, 98, 100, "bull", 0.5, 0.5) < ms.conviction(80, 1.0, 101, 100, "bull", 0.5, 0.5)
    assert ms.conviction(80, 1.0, 99, 100, "bear", 0.5, 0.5) > ms.conviction(80, 1.0, 101, 100, "bear", 0.5, 0.5)
    w = ms.parse_weights("strength=1,volume=0,vwap=0,sector=0,zone=0,bogus=5,x")
    assert w == {"strength": 1.0, "volume": 0.0, "vwap": 0.0, "sector": 0.0, "zone": 0.0}
    assert ms.conviction(60, 0, 100, None, "bull", 0, 0, w) == 60


def test_aggregate_atr_vwap_pace_range_profile_spark():
    t0 = 1_790_000_100 - 1_790_000_100 % 300
    ones = [(t0 + i * 60, 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(7)]
    out = ms.aggregate(ones, 5)
    assert len(out) == 2 and out[0] == (t0, 100, 105, 99, 104.5, 50) and out[1][5] == 20
    assert ms.aggregate([(60.0, 1, 2, 0.5, 1.5, 3)], 1) == [(60.0, 1, 2, 0.5, 1.5, 3)]
    assert ms.atr([bar(0, 1, 2, 1, 1.5)]) is None and ms.atr(quiet(4)) is None
    assert ms.atr(quiet(12)) == pytest.approx(0.4, abs=0.15)
    bars = [bar(0, 10, 12, 8, 10, 100), bar(1, 20, 22, 18, 20, 300)]
    assert ms.vwap_from_bars(bars) == pytest.approx((10 * 100 + 20 * 300) / 400)
    assert ms.vwap_from_bars([bar(0, 10, 12, 8, 10, 0)]) is None
    vb = [bar(i, 1, 1, 1, 1, 10) for i in range(30)] + [bar(30 + i, 1, 1, 1, 1, 30) for i in range(15)]
    assert ms.volume_pace(vb, 15) == pytest.approx(3.0) and ms.volume_pace(vb[:20], 15) is None
    t = 1_000_000.0
    ob = [(t + i * 60, 10, 10 + i, 9 - i * 0.1, 10, 5) for i in range(30)]
    lo, hi = ms.opening_range(ob, t, 15)
    assert hi == 24 and lo == pytest.approx(7.6) and ms.opening_range(ob, t + 10_000, 15) is None
    pb = [bar(0, 10, 11, 9, 9.5, 100), bar(1, 10, 11, 9, 10.5, 50), bar(2, 10, 11, 9, 10.6, 50)]
    p = ms.profile(pb, 4)
    assert max(p) == 1.0 and len(p) == 4
    assert max(ms.profile([bar(0, 10, 11, 9, 9.5, 0), bar(1, 10, 11, 9, 10.5, 0)], 4)) == 1.0
    assert ms.profile([], 4) == [] and ms.profile([bar(0, 10, 10, 10, 10, 1)], 4) == []
    sb = [bar(i, i, i, i, i, 1) for i in range(500)]
    sp = ms.spark(sb, 60)
    assert len(sp) == 60 and sp[-1] == 499 and ms.spark(sb[:10], 60) == [float(i) for i in range(10)]


def test_swings():
    b = [bar(0, 1, 1.0, 0.9, 1), bar(1, 1, 1.1, 0.9, 1), bar(2, 1, 1.5, 0.95, 1), bar(3, 1, 1.1, 0.9, 1), bar(4, 1, 1.0, 0.9, 1),
         bar(5, 1, 1.0, 0.5, 1), bar(6, 1, 1.0, 0.8, 1), bar(7, 1, 1.0, 0.85, 1)]
    s = ms.swings(b)
    assert {"idx": 2, "t": b[2][0], "kind": "high", "price": 1.5} in s and {"idx": 5, "t": b[5][0], "kind": "low", "price": 0.5} in s


def test_zones():
    zs = ms.find_zones(bullish_series())
    assert len(zs) == 1
    z = zs[0]
    assert z["side"] == "bull" and z["fvg"] and z["valid"] and z["status"] == "fresh"
    assert z["bottom"] == pytest.approx(99.7) and z["top"] == pytest.approx(100.15)
    assert ms.find_zones(bullish_series(), full_range=True)[0]["bottom"] == pytest.approx(99.6)
    assert ms.find_zones(quiet(20)) == [] and ms.find_zones(quiet(3)) == []


def test_zone_tested_then_broken_and_cap():
    bars = bullish_series()
    n = len(bars)
    bars.append(bar(n, 103.0, 103.0, 100.0, 100.4))
    assert ms.find_zones(bars)[0]["status"] == "tested"
    bars.append(bar(n + 1, 100.4, 100.5, 99.0, 99.1))
    assert [z for z in ms.find_zones(bars) if z["side"] == "bull"] == []
    b2 = bullish_series()
    n = len(b2)
    b2.append(bar(n, 104.2, 105.0, 104.0, 104.9)); b2.append(bar(n + 1, 104.8, 104.9, 100.9, 101.1)); b2.append(bar(n + 2, 101.1, 101.6, 100.4, 100.7))
    zs = ms.find_zones(b2)
    assert [z["side"] for z in zs] == ["bull", "bear"] and zs == sorted(zs, key=lambda z: z["t"])
    assert len(ms.find_zones(b2, max_zones=1)) == 1 and ms.find_zones(b2, max_zones=1)[0]["side"] == "bear"
    assert ms.find_zones(b2, max_zones=0) == []


def test_bos():
    bars = quiet(4)
    bars.append(bar(4, 100.0, 100.9, 99.9, 100.2))
    bars += [bar(5, 100.2, 100.3, 99.9, 100.0), bar(6, 100.0, 100.3, 99.8, 100.1)]
    bars += quiet(3, start=7)
    n = len(bars)
    bars.append(bar(n, 100.1, 100.15, 99.6, 99.7)); bars.append(bar(n + 1, 99.75, 102.6, 99.7, 102.5)); bars.append(bar(n + 2, 102.5, 103.0, 102.2, 102.8))
    assert ms.find_zones(bars)[0]["bos"] is True


def test_quality_pick_chase():
    z = {"side": "bull", "top": 100, "bottom": 99, "t": 1, "valid": True, "bos": True, "fvg": True, "status": "fresh", "atr": 0.5}
    assert ms.zone_quality(z) == 1.0 and ms.zone_quality(None) is None
    weak = dict(z, bos=False, fvg=False, valid=False, status="tested", t=2)
    assert ms.zone_quality(weak) == 0.4 and ms.pick_zone([weak, z], "bull", 100.5) is z and ms.pick_zone([z], "bear", 100.5) is None
    assert ms.chase_atr("bull", 101.0, z) == pytest.approx(2.0) and ms.chase_atr("bull", 99.9, z) < 0
    zb = {"side": "bear", "top": 101.0, "bottom": 100.0, "atr": 0.5}
    assert ms.chase_atr("bear", 99.0, zb) == pytest.approx(2.0)
    assert ms.chase_atr("bull", 101.0, None) is None and ms.chase_atr("bull", 101.0, {"top": 1, "bottom": 0, "atr": 0}) is None


def test_levels_and_rr():
    bars = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 102, 100, 101), bar(2, 101, 101.5, 100.6, 101.2),
            bar(3, 101.2, 101.6, 100.8, 101.0), bar(4, 101.0, 101.4, 100.7, 101.1)]
    sw = [{"idx": 1, "t": bars[1][0], "kind": "high", "price": 102.0}]
    lv = ms.levels(101.0, sw, bars, prev_day={"high": 103.0, "low": 98.0, "close": 99.0}, or_range=(100.0, 101.8))
    assert [x["price"] for x in lv["resistances"]] == [101.8, 102.0, 103.0] and [x["label"] for x in lv["resistances"]] == ["R1", "R2", "R3"]
    assert [x["price"] for x in lv["supports"]] == [100.0, 99.0, 98.0] and lv["supports"][0]["label"] == "S1"
    fb = [bar(0, 1, 5, 1, 2), bar(1, 2, 6, 1, 5)]
    assert ms.levels(3.0, [{"idx": 0, "t": 0, "kind": "high", "price": 5.0}], fb)["resistances"] == []
    assert len(ms.levels(100.0, [], [], prev_day={"high": 101.0, "low": 90, "close": 101.05})["resistances"]) == 1
    z = {"side": "bull", "top": 100.0, "bottom": 99.0, "atr": 1}
    l2 = {"resistances": [{"price": 103.7, "label": "R1"}], "supports": [{"price": 96.0, "label": "S1"}]}
    rr = ms.risk_reward("bull", 101.0, z, l2)
    assert rr["rr"] == 1.35 and rr["target_label"] == "R1" and rr["invalidation"] == 99.0
    rb = ms.risk_reward("bear", 100.0, {"side": "bear", "top": 102.0, "bottom": 101.0, "atr": 1}, l2)
    assert rb["rr"] == 2.0 and rb["target"] == 96.0
    assert ms.risk_reward("bull", 101.0, None, {}) is None
    assert ms.risk_reward("bull", 98.0, z, {"resistances": [{"price": 105, "label": "R1"}]}) is None
    assert ms.risk_reward("bull", 101.0, z, {"resistances": []}) is None
