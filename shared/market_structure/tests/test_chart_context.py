"""E25 / E26: chart_context (candlestick chart + pattern / regime badges). Synthetic bars, no network, no clock."""
import datetime as _dt, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure import chart_context as CC
from market_structure.chart_context import chart_context, CANDLE_INFO, CHART_INFO

T0 = 1_700_000_000; D = 86400


def path(wps, start=None, wick=0.3, vol=1000.0):
    px = start if start is not None else wps[0][1]
    out = []
    for n, tgt in wps:
        for k in range(1, n + 1):
            c = px + (tgt - px) * k / n
            o = out[-1][4] if out else c
            out.append((T0 + len(out) * D, o, max(o, c) + wick, min(o, c) - wick, c, vol))
        px = tgt
    return out


def dates_for(bars):
    d0 = _dt.date(2025, 1, 1)
    return [(d0 + _dt.timedelta(days=i)).isoformat() for i in range(len(bars))]


def walk(n, seed=5):
    rnd = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        o, c = px, max(5.0, px + rnd.gauss(0.05, 1.2))
        out.append((T0 + i * D, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1000.0))
        px = c
    return out


def test_every_guide_pattern_has_a_label_and_a_tip():
    guide_candles = {"hammer", "inverted_hammer", "bullish_engulfing", "morning_star", "three_white_soldiers", "tweezer_bottom", "shooting_star",
                     "hanging_man", "bearish_engulfing", "evening_star", "three_black_crows", "tweezer_top", "doji", "dragonfly_doji", "gravestone_doji", "spinning_top"}
    guide_chart = {"double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders", "rising_wedge", "falling_wedge", "ascending_triangle",
                   "descending_triangle", "symmetrical_triangle", "bull_flag", "bear_flag", "cup_and_handle"}
    assert guide_candles <= set(CANDLE_INFO) and guide_chart <= set(CHART_INFO)
    assert all(len(v) == 2 and v[0] and v[1] for v in list(CANDLE_INFO.values()) + list(CHART_INFO.values()))


def test_shape_and_bars_roundtrip():
    bars = walk(300)
    ds = dates_for(bars)
    out = chart_context(bars, ds, tail=120)
    assert out["ok"] and out["context_only"] and out["errors"] == []
    assert len(out["bars"]) == 120 and out["bars"][-1][0] == ds[-1] == out["last_bar"]
    assert set(out["regime"]) == {"trend", "volatility", "momentum", "structure"}
    assert len(out["badges"]) <= CC.MAX_BADGES


def test_never_raises_and_degrades_on_bad_input():
    assert chart_context(None, None)["ok"] is False
    assert chart_context([], [])["bars"] == []
    assert chart_context(walk(3), dates_for(walk(3)))["ok"] is False
    bars = walk(100)
    ds = dates_for(bars)
    bars[40] = (bars[40][0], float("nan"), 1, 1, 1, 1)
    ds[41] = None
    out = chart_context(bars, ds)
    assert out["ok"] and len(out["bars"]) == 98 and out["errors"] == []
    assert chart_context(123, "x")["ok"] is False


def test_dates_are_the_inputs_not_derived_from_epoch():
    bars = walk(60)
    ds = ["2026-01-%02d" % (1 + i % 28) for i in range(60)]
    out = chart_context(bars, ds)
    assert [b[0] for b in out["bars"]] == ds


def test_candle_marker_and_badge_sit_on_the_right_bar():
    lead = path([(40, 100.0)], start=160.0, wick=0.5)
    bars = lead + [(T0 + 40 * D, 100.0, 100.5, 98.0, 100.4, 1000.0)]
    ds = dates_for(bars)
    out = chart_context(bars, ds)
    hm = [m for m in out["markers"] if m["name"] == "hammer"]
    assert hm and hm[0]["date"] == ds[-1] and hm[0]["bars_ago"] == 0
    b = [x for x in out["badges"] if x["key"] == "hammer"][0]
    assert b["tone"] == "good" and "today" in b["tip"]


def test_double_top_shape_lines_are_inside_the_window_and_drawable():
    bars = path([(30, 80), (20, 100), (10, 90), (10, 100.3), (12, 84)], start=80)
    ds = dates_for(bars)
    out = chart_context(bars, ds, tail=60)
    sh = [s for s in out["shapes"] if s["name"] == "double_top"][0]
    assert sh["status"] == "confirmed" and sh["bias"] == "bearish" and sh["tip"]
    roles = {l["role"] for l in sh["lines"]}
    assert "neckline" in roles and "target" in roles
    window = set(b[0] for b in out["bars"])
    for l in sh["lines"]:
        assert l["x1"] in window and l["x2"] in window and l["x1"] <= l["x2"]
    assert any(b["key"] == "double_top" and b["tone"] == "bad" for b in out["badges"])


def test_clipping_keeps_a_line_that_starts_before_the_window():
    ds = dates_for(path([(100, 100.0)], start=100.0))
    seg = CC._seg(ds, 50, 100, "neckline", 10, 100.0, 99, 120.0)
    assert seg["x1"] == ds[50] and abs(seg["y1"] - (100.0 + 20.0 * 40 / 89)) < 0.02 and seg["x2"] == ds[99]
    assert CC._seg(ds, 50, 100, "x", 10, 1.0, 40, 2.0) is None
    assert CC._seg(ds, 0, 100, "x", 0, float("nan"), 9, 1.0) is None


def test_triangle_and_flag_produce_shapes():
    asc = path([(20, 90), (6, 100), (6, 90), (6, 100), (6, 94), (6, 100), (6, 97), (3, 99)], start=90)
    out = chart_context(asc, dates_for(asc))
    tri = [s for s in out["shapes"] if s["name"] == "ascending_triangle"][0]
    assert {l["role"] for l in tri["lines"]} == {"upper", "lower"}
    flag = path([(30, 80), (12, 120), (8, 114)], start=80)
    out = chart_context(flag, dates_for(flag))
    bf = [s for s in out["shapes"] if s["name"] == "bull_flag"][0]
    assert {"upper", "lower", "target"} <= {l["role"] for l in bf["lines"]}


def test_regime_and_badge_ordering():
    up = path([(260, 200.0)], start=100.0, wick=0.4)
    out = chart_context(up, dates_for(up), structure_trend="bullish")
    assert out["regime"]["trend"] == "up" and out["regime"]["structure"] == "bullish"
    assert [b["kind"] for b in out["badges"]][0] == "regime"
    assert [b for b in out["badges"] if b["key"] == "regime_trend"][0]["label"] == "Uptrend"
    short = chart_context(up[:80], dates_for(up[:80]))
    assert [b for b in short["badges"] if b["key"] == "regime_trend"][0]["label"].startswith("Short-term")
    assert chart_context(up[:30], dates_for(up[:30]))["regime"]["trend"] is None
    assert chart_context(up, dates_for(up), structure_trend="weird")["regime"]["structure"] is None


def test_a_failing_pattern_engine_is_isolated():
    orig = CC.analyze_patterns
    try:
        CC.analyze_patterns = lambda *_a, **_k: (_ for _ in ()).throw(ValueError("boom"))
        out = chart_context(walk(100), dates_for(walk(100)))
        assert out["bars"] and any("context:ValueError" in e for e in out["errors"])
    finally:
        CC.analyze_patterns = orig


# ---- E29: uncapped `events` for the watchlist digest ----
def test_events_hold_every_signal_and_confirmed_pattern_even_when_badges_are_capped():
    import market_structure.chart_context as cc
    from _bars import walk
    seen = 0
    for seed in range(40):
        bars = walk(320, seed=seed, drift=(-0.2, 0.0, 0.3)[seed % 3])
        out = cc.chart_context(bars, [f"2025-01-{1 + i % 28:02d}" for i in range(len(bars))])
        want = [b["key"] for b in out["badges"] if b["kind"] == "signal" or (b["kind"] == "chart" and b.get("status") == "confirmed")]
        got = [e["key"] for e in out["events"]]
        assert all(k in got for k in want)                                           # nothing the chart shows is missing from the events
        assert all(e["kind"] == "signal" or (e["kind"] == "chart" and e["status"] == "confirmed") for e in out["events"])
        assert set(out["events"][0]) == {"key", "kind", "label", "tone", "status", "bars_ago"} if out["events"] else True
        seen += len(got)
        old = cc.MAX_BADGES
        cc.MAX_BADGES = 1                                                            # a crowded chart: only one badge survives
        try:
            small = cc.chart_context(bars, [f"2025-01-{1 + i % 28:02d}" for i in range(len(bars))])
        finally:
            cc.MAX_BADGES = old
        assert len(small["badges"]) <= 1 and [e["key"] for e in small["events"]] == got
    assert seen > 0                                                                 # the synthetic series do produce signals
    assert chart_context_empty_has_events()


def chart_context_empty_has_events():
    import market_structure.chart_context as cc
    return cc.chart_context([], []).get("events") == []


# ---------------------------------------------------------------- E25b: Fibonacci levels on the chart
def _fib_block(direction="bullish", lo=100.0, hi=200.0, **kw):
    b = {"swing_low": lo, "swing_high": hi, "direction": direction, "retracement": 0.55, "in_golden_zone": True, "nearest_level": 0.5, "distance_pct": 1.0,
         "confluence_count": 1, "leg_invalidated": False, "golden_low": None, "golden_high": None, "ext_1272": None, "ext_1618": None}
    b.update(kw)
    return b


def _by_label(fib):
    return {x["label"]: x for x in fib["levels"]}


def test_fib_levels_bullish_leg_geometry():
    f = CC.fib_levels(_fib_block("bullish", 100.0, 200.0))
    L = _by_label(f)
    assert (L["0%"]["price"], L["100%"]["price"]) == (200.0, 100.0)                  # a bullish leg: 0 % at the high, 100 % at the low
    assert (L["23.6%"]["price"], L["38.2%"]["price"], L["50%"]["price"], L["61.8%"]["price"], L["78.6%"]["price"]) == (176.4, 161.8, 150.0, 138.2, 121.4)
    assert (L["127.2%"]["price"], L["161.8%"]["price"]) == (227.2, 261.8)             # extensions run past the high
    assert f["golden"] == {"low": 138.2, "high": 150.0} and f["in_golden_zone"] is True and f["invalidated"] is False and f["source"] == "daily"
    assert [x["price"] for x in f["levels"]] == sorted((x["price"] for x in f["levels"]), reverse=True)
    assert {x["kind"] for x in f["levels"] if x["label"] in ("50%", "61.8%")} == {"golden"}


def test_fib_levels_bearish_leg_geometry():
    f = CC.fib_levels(_fib_block("bearish", 100.0, 200.0, in_golden_zone=False, leg_invalidated=True))
    L = _by_label(f)
    assert (L["0%"]["price"], L["100%"]["price"]) == (100.0, 200.0)                  # a bearish leg: 0 % at the low, 100 % at the high
    assert (L["50%"]["price"], L["61.8%"]["price"]) == (150.0, 161.8)
    assert (L["127.2%"]["price"], L["161.8%"]["price"]) == (72.8, 38.2)               # extensions run below the low
    assert f["golden"] == {"low": 150.0, "high": 161.8} and f["in_golden_zone"] is False and f["invalidated"] is True


def test_fib_levels_use_the_engine_extensions_when_given():
    f = CC.fib_levels(_fib_block("bullish", 100.0, 200.0, ext_1272=227.5, ext_1618=262.0))
    L = _by_label(f)
    assert (L["127.2%"]["price"], L["161.8%"]["price"]) == (227.5, 262.0)


def test_fib_levels_empty_or_broken_blocks_give_none():
    for bad in (None, {}, "x", _fib_block(lo=None), _fib_block(hi=None), _fib_block(lo=200.0, hi=200.0), _fib_block(lo=200.0, hi=100.0),
                _fib_block(lo=float("nan")), _fib_block(direction=None), _fib_block(lo=0.0)):
        assert CC.fib_levels(bad) is None


def test_chart_context_carries_fib_and_is_none_without_it():
    bars = walk(120)
    ds = dates_for(bars)
    assert chart_context(bars, ds)["fib"] is None
    out = chart_context(bars, ds, fib=_fib_block("bullish", 100.0, 200.0))
    assert out["fib"]["direction"] == "bullish" and len(out["fib"]["levels"]) == 9
    assert chart_context(bars, ds, fib={"swing_low": "junk"})["fib"] is None and chart_context([], [])["fib"] is None
    assert out["badges"] == chart_context(bars, ds)["badges"] and out["events"] == chart_context(bars, ds)["events"]   # display only
