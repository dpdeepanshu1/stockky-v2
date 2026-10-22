"""C18-C20: candlestick / chart patterns, squeeze, crosses, MACD+RSI confluence, S/R flip (patterns.py). Context only; synthetic bars, no network, no clock."""
import math, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure import patterns as P
from market_structure.extras import analyze_extras

T0=1_700_000_000; D=86400
def path(wps, start=None, wick=0.3, vol=1000.0):
    # wps: list of (n_bars, target_price); linear from the previous target
    px = start if start is not None else wps[0][1]
    out=[]; i=0
    for n,tgt in wps:
        for k in range(1,n+1):
            c = px + (tgt-px)*k/n
            o = out[-1][4] if out else c
            out.append((T0+len(out)*D, o, max(o,c)+wick, min(o,c)-wick, c, vol))
        px = tgt
    return out
def flip(bars,K=400.0): return [(b[0],K-b[1],K-b[3],K-b[2],K-b[4],b[5]) for b in bars]


def _decline(end=100.0, n=40, step=1.5):
    """n bars falling `step` a day into `end` (a clean prior down-move)."""
    return path([(n, end)], start=end + n * step, wick=0.5)


def _names(bars, lookback=1):
    return {c["name"] for c in P.candle_patterns(bars, lookback)}


def _bar(prev_close, o, h, l, c, i):
    return (T0 + i * D, o, h, l, c, 1000.0)


def _after(lead, rows):
    out = list(lead)
    for r in rows:
        out.append((T0 + len(out) * D,) + tuple(r) + (1000.0,))
    return out


# ---- candlesticks: each bullish reversal on a decline; flipping the prices gives the mirrored bearish pattern on a rally
def _bear_leg_lead():
    """40 falling bars whose last bar is a bearish 1.5-point body (open 103.0, close 101.5)."""
    lead = _decline(end=101.5, n=40, step=1.5)
    return lead[:-1] + [(lead[-1][0], 103.0, 103.3, 101.2, 101.5, 1000.0)]


CANDLE_CASES = [
    # bull name, mirrored bear name, lead, rows (o, h, l, c)
    ("hammer", "shooting_star", lambda: _decline(end=100.0), [(100.0, 100.5, 98.0, 100.4)]),
    ("inverted_hammer", "hanging_man", lambda: _decline(end=100.0), [(100.0, 102.5, 99.9, 100.3)]),
    ("bullish_engulfing", "bearish_engulfing", _bear_leg_lead, [(99.8, 103.6, 99.5, 103.3)]),
    ("bullish_harami", "bearish_harami", _bear_leg_lead, [(101.7, 102.4, 101.5, 102.2)]),
    ("piercing_line", "dark_cloud_cover", _bear_leg_lead, [(101.0, 102.9, 100.8, 102.7)]),
    ("morning_star", "evening_star", lambda: _decline(end=105.0), [(105.0, 105.2, 99.8, 100.0), (100.0, 100.2, 99.5, 99.8), (100.0, 103.2, 99.9, 103.0)]),
]


def test_candle_reversals_and_their_mirrors():
    for bull, bear, lead_fn, rows in CANDLE_CASES:
        bars = _after(lead_fn(), rows)
        got = _names(bars, lookback=len(rows))
        assert bull in got, (bull, got)
        assert bear in _names(flip(bars), lookback=len(rows)), (bear,)


def test_hammer_needs_the_prior_move_and_doji_marubozu_do_not():
    flat = path([(40, 100.0)], start=100.0, wick=0.5)
    assert "hammer" not in _names(_after(flat, [(100.0, 100.5, 98.0, 100.4)]))
    assert "hanging_man" not in _names(_after(flat, [(100.0, 100.5, 98.0, 100.4)]))
    assert "doji" in _names(_after(flat, [(100.0, 101.5, 98.5, 100.05)]))
    mb = P.candle_patterns(_after(flat, [(100.0, 104.1, 99.9, 104.0)]), 1)
    assert [c for c in mb if c["name"] == "marubozu"][0]["bias"] == "bullish"


def test_three_soldiers_and_crows():
    rows = [(100.0, 103.3, 99.7, 103.0), (101.5, 105.3, 101.2, 105.0), (103.5, 107.3, 103.2, 107.0)]
    lead = path([(40, 100.0)], start=100.0, wick=0.5)
    bars = _after(lead, rows)
    assert "three_white_soldiers" in _names(bars)
    assert "three_black_crows" in _names(flip(bars))


def test_volume_confirmation_flag():
    lead = _decline(end=100.0)
    bars = _after(lead, [(100.0, 100.5, 98.0, 100.4)])
    assert P.candle_patterns(bars, 1)[0]["volume_confirmed"] is False
    b = bars[-1]
    bars[-1] = (b[0], b[1], b[2], b[3], b[4], 5000.0)
    assert [c for c in P.candle_patterns(bars, 1) if c["name"] == "hammer"][0]["volume_confirmed"] is True


# ---- chart patterns
def _chart(bars):
    return {c["name"]: c for c in P.chart_patterns(bars)}


def test_double_top_and_bottom_with_neckline_and_target():
    b = path([(30, 80), (20, 100), (10, 90), (10, 100.3), (12, 84)], start=80)
    dt = _chart(b)["double_top"]
    assert dt["status"] == "confirmed" and dt["target"] < dt["neckline"]
    assert _chart(flip(b))["double_bottom"]["status"] == "confirmed"
    forming = path([(30, 80), (20, 100), (10, 90), (10, 100.3), (4, 97)], start=80)
    assert _chart(forming)["double_top"]["status"] == "forming"


def test_head_and_shoulders_and_inverse():
    b = path([(30, 80), (20, 100), (8, 92), (10, 110), (8, 92), (10, 100.2), (12, 84)], start=80)
    assert _chart(b)["head_and_shoulders"]["status"] == "confirmed"
    assert "inverse_head_and_shoulders" in _chart(flip(b))


def test_triangles_wedges_rectangle_flags():
    asc = [(20, 90), (6, 100), (6, 90), (6, 100), (6, 94), (6, 100), (6, 97), (3, 99)]
    assert _chart(path(asc, start=90))["ascending_triangle"]["bias"] == "bullish"
    assert _chart(flip(path(asc, start=90)))["descending_triangle"]["bias"] == "bearish"
    assert "symmetrical_triangle" in _chart(path([(20, 95), (6, 110), (6, 90), (6, 106), (6, 94), (6, 102), (6, 97), (3, 99)], start=95))
    assert "rectangle" in _chart(path([(20, 95), (6, 110), (6, 95), (6, 110), (6, 95), (6, 110), (6, 95), (3, 100)], start=95))
    wedge = [(20, 90), (6, 100), (6, 92), (6, 104), (6, 97), (6, 107), (6, 102), (3, 105)]
    assert _chart(path(wedge, start=90))["rising_wedge"]["bias"] == "bearish"
    assert _chart(flip(path(wedge, start=90)))["falling_wedge"]["bias"] == "bullish"
    flag = [(30, 80), (12, 120), (8, 114)]
    bf = _chart(path(flag, start=80))["bull_flag"]
    assert bf["target"] > 114 and bf["status"] == "forming"
    assert "bear_flag" in _chart(flip(path(flag, start=80)))


def test_flag_confirms_on_breakout_close():
    b = path([(30, 80), (12, 120), (8, 114), (2, 124)], start=80)
    assert _chart(b).get("bull_flag", {}).get("status") == "confirmed"


def test_every_chart_pattern_name_is_covered():
    names = {"double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders", "ascending_triangle", "descending_triangle",
             "symmetrical_triangle", "rising_wedge", "falling_wedge", "rectangle", "bull_flag", "bear_flag", "cup_and_handle"}
    assert len(names) == 13
    src = open(P.__file__, encoding="utf-8").read()
    assert all('"%s"' % n in src for n in names)


# ---- no lookahead: pivots on a truncated series equal the full series' pivots that were already confirmed
def test_pivots_do_not_use_future_bars():
    bars = walk_bars(260, seed=11)
    full = P._pivots(bars)
    for k in (60, 110, 175, 240):
        ph, pl = P._pivots(bars[:k])
        assert ph == [p for p in full[0] if p[0] <= k - 1 - P.PIVOT_K]
        assert pl == [p for p in full[1] if p[0] <= k - 1 - P.PIVOT_K]


def walk_bars(n, seed=5, base=100.0, vol=1.2):
    rnd = random.Random(seed)
    px, out = base, []
    for i in range(n):
        o, c = px, max(5.0, px + rnd.gauss(0.05, vol))
        out.append((T0 + i * D, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1000.0))
        px = c
    return out


# ---- indicators
def test_bollinger_squeeze_and_release():
    rnd = random.Random(3)
    loud = [100 + (3 if i % 2 else -3) + rnd.gauss(0, 0.5) for i in range(150)]
    calm = [100 + rnd.gauss(0, 0.05) for _ in range(30)]
    def mk(closes):
        return [(T0 + i * D, c, c + 0.2, c - 0.2, c, 1000.0) for i, c in enumerate(closes)]
    sq = P.bollinger_squeeze(mk(loud + calm))
    assert sq["squeeze"] is True and sq["percentile"] <= 10 and sq["release"] is None
    rel = P.bollinger_squeeze(mk(loud + calm + [106.0]))
    assert rel["squeeze"] is False and rel["release"] == "bullish"
    assert P.bollinger_squeeze(mk(loud[:50])) is None            # not enough history


def test_golden_and_death_cross_events():
    up = path([(200, 100.0), (120, 160.0)], start=200.0, wick=0.3)
    closes = [b[4] for b in up]
    k = next(i for i in range(201, len(closes)) if P._sma(closes, 50, i) > P._sma(closes, 200, i) and P._sma(closes, 50, i - 1) <= P._sma(closes, 200, i - 1))
    r = P.ma_crosses(up[:k + 3])["ma50_200"]
    assert r["event"] == {"type": "golden_cross", "bars_ago": 2} and r["fast_above_slow"] is True
    r2 = P.ma_crosses(flip(up)[:k + 3])["ma50_200"]
    assert r2["event"] == {"type": "death_cross", "bars_ago": 2} and r2["fast_above_slow"] is False
    assert P.ma_crosses(up[:100])["ma50_200"] is None and P.ma_crosses(up[:100])["ma20_50"] is not None
    assert P.ma_crosses(up[:k - 20])["ma50_200"]["event"] is None


def test_macd_rsi_confluence_is_symmetric_and_neutral_when_overbought():
    bars = walk_bars(300, seed=21)
    seen = set()
    for k in range(60, 300):
        a, b = P.macd_rsi_confluence(bars[:k]), P.macd_rsi_confluence(flip(bars[:k], K=1000.0))
        mirror = {"bullish": "bearish", "bearish": "bullish", "neutral": "neutral"}
        assert b["state"] == mirror[a["state"]], (k, a, b)
        seen.add(a["state"])
    assert {"bullish", "bearish"} <= seen
    straight = path([(80, 200.0)], start=100.0)
    assert P.macd_rsi_confluence(straight)["state"] == "neutral"       # RSI pinned near 100: not a 50-70 setup
    assert P.macd_rsi_confluence(bars[:30]) is None


def test_support_and_resistance_flip():
    b = path([(30, 90), (10, 110), (5, 104), (8, 120), (6, 110.6), (4, 114)], start=90)
    f = P.sr_flips(b)
    assert f and f[0]["type"] == "support_flip" and abs(f[0]["level"] - 110.3) < 0.5
    g = P.sr_flips(flip(b))
    assert g and g[0]["type"] == "resistance_flip"
    failed = path([(30, 90), (10, 110), (5, 104), (8, 120), (6, 110.6), (4, 104)], start=90)   # fell back through the level: no flip
    assert not any(x["type"] == "support_flip" for x in P.sr_flips(failed))


# ---- wrapper
def test_analyze_patterns_shape_and_robustness():
    out = P.analyze_patterns(walk_bars(300, seed=4))
    assert out["context_only"] is True and out["errors"] == []
    assert isinstance(out["candles"], list) and isinstance(out["chart"], list) and set(out["crosses"]) == {"ma50_200", "ma20_50"}
    short = P.analyze_patterns(walk_bars(10))
    assert short["candles"] is None and short["errors"] == []
    assert P.analyze_patterns(None)["bars"] == 0 and P.analyze_patterns([])["errors"] == []
    junk = walk_bars(80, seed=2)
    junk[40] = (junk[40][0], float("nan"), 1, 1, 1, 1)
    junk[41] = "bad"
    assert P.analyze_patterns(junk)["bars"] == 78
    flat = [(T0 + i * D, 100.0, 100.0, 100.0, 100.0, 0.0) for i in range(120)]
    assert P.analyze_patterns(flat)["errors"] == []                    # zero range / zero ATR / zero volume never divides


def test_extras_carries_patterns_and_isolates_a_failure():
    ex = analyze_extras(walk_bars(300, seed=4))
    assert ex["patterns"]["context_only"] is True and ex["errors"] == []
    orig = P.analyze_patterns
    import market_structure.extras as em
    try:
        em.analyze_patterns = lambda *_a, **_k: (_ for _ in ()).throw(ValueError("boom"))
        r = analyze_extras(walk_bars(300, seed=4))
        assert r["patterns"] is None and "patterns:ValueError" in r["errors"] and r["features_v2"] is not None
    finally:
        em.analyze_patterns = orig



# ---- whyXShubh guide additions: dragonfly / gravestone doji, spinning top, tweezers, cup and handle
def test_dragonfly_gravestone_and_spinning_top():
    flat = path([(40, 100.0)], start=100.0, wick=0.5)
    assert "dragonfly_doji" in _names(_after(flat, [(100.0, 100.05, 97.0, 100.02)]))
    assert "doji" not in _names(_after(flat, [(100.0, 100.05, 97.0, 100.02)]))
    assert "gravestone_doji" in _names(_after(flat, [(100.0, 103.0, 99.95, 100.0)]))
    assert "spinning_top" in _names(_after(flat, [(100.0, 101.6, 98.4, 100.6)]))
    assert "doji" in _names(_after(flat, [(100.0, 101.5, 98.5, 100.05)]))
    assert "spinning_top" not in _names(_after(flat, [(100.0, 100.12, 99.9, 100.04)]))


def test_tweezer_bottom_and_top_need_the_prior_move_and_matching_extreme():
    lead = _bear_leg_lead()
    bars = _after(lead, [(101.6, 103.0, 101.2, 102.8)])
    assert "tweezer_bottom" in _names(bars)
    assert "tweezer_top" in _names(flip(bars))
    miss = _after(lead, [(101.6, 103.0, 100.4, 102.8)])
    assert "tweezer_bottom" not in _names(miss)
    flat = path([(40, 100.0)], start=100.0, wick=0.5)
    assert "tweezer_bottom" not in _names(_after(flat, [(100.0, 100.4, 99.0, 99.3), (99.4, 100.6, 99.0, 100.3)]), 2)


def _cup(handle_to=97.0, tail=None):
    """Rise to a 100 rim, a U-shaped cup (80 at the bottom) back to 100, then a short dip (the handle)."""
    bars = path([(30, 100.0)], start=80.0, wick=0.3)
    n = 40
    for k in range(1, n + 1):
        t = k / n
        c = 100.0 - 20.0 * math.sin(math.pi * t) ** 0.5
        o = bars[-1][4]
        bars.append((T0 + len(bars) * D, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000.0))
    for tgt in ([98.0, 97.0, 96.5, handle_to] if tail is None else tail):
        o = bars[-1][4]
        bars.append((T0 + len(bars) * D, o, max(o, tgt) + 0.3, min(o, tgt) - 0.3, tgt, 1000.0))
    return bars


def test_cup_and_handle_forming_then_confirmed_and_geometry():
    cup = _chart(_cup())["cup_and_handle"]
    assert cup["bias"] == "bullish" and cup["status"] == "forming" and cup["target"] > cup["neckline"] > 95.0
    assert cup["start"] < cup["rim_right"] and cup["cup_low"] < cup["neckline"] - 10
    done = _chart(_cup(tail=[98.0, 97.0, 98.5, 101.5, 102.0]))
    assert done.get("cup_and_handle", {}).get("status") == "confirmed"


def test_cup_needs_a_u_shape_and_level_rims():
    v = path([(30, 100.0), (20, 80.0), (20, 100.0), (4, 97.0)], start=80.0, wick=0.3)
    assert "cup_and_handle" not in _chart(v)
    half = path([(30, 100.0), (20, 80.0), (20, 90.0), (4, 88.0)], start=80.0, wick=0.3)
    assert "cup_and_handle" not in _chart(half)


def test_chart_patterns_carry_a_start_index():
    b = path([(30, 80), (20, 100), (10, 90), (10, 100.3), (12, 84)], start=80)
    dt = _chart(b)["double_top"]
    assert isinstance(dt["start"], int) and 0 <= dt["start"] < len(b)
    asc = [(20, 90), (6, 100), (6, 90), (6, 100), (6, 94), (6, 100), (6, 97), (3, 99)]
    tri = _chart(path(asc, start=90))["ascending_triangle"]
    assert tri["upper0"] is not None and tri["lower0"] is not None and tri["start"] < len(asc) * 6
