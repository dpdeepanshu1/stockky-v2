"""group441 (WS-15): intraday structure from recorded 1-minute bars. Session clock, partial bars, completeness, VWAP, params, no lookahead."""
import os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from market_structure import intraday as it
from market_structure.params import MSParams, params_hash

IST = 19800
D0 = 20000


def open_ts(day):
    return day * 86400 - IST + 33300


def day_minutes(day, seed=1, base=100.0, n=375, start=0, vol=0.002, drift=0.0):
    rnd = random.Random(seed * 1000 + day)
    px, out = base, []
    for m in range(start, start + n):
        o = px
        c = max(1.0, o * (1 + rnd.gauss(drift, vol)))
        out.append((open_ts(day) + m * 60, o, max(o, c) * 1.0007, min(o, c) * 0.9993, c, 1000.0 + m))
        px = c
    return out


def days(k, seed=1, **kw):
    out, base = [], 100.0
    for d in range(k):
        bars = day_minutes(D0 + d, seed, base, **kw)
        out += bars
        base = bars[-1][4]
    return out


def test_buckets_are_session_aligned_and_ohlcv_is_right():
    b = day_minutes(D0)
    agg = it.aggregate_session(b, 15)
    assert len(agg) == 25 and agg[0][0] == open_ts(D0) and agg[1][0] == open_ts(D0) + 900
    first = b[:15]
    assert agg[0][1] == first[0][1] and agg[0][4] == first[-1][4]
    assert agg[0][2] == max(x[2] for x in first) and agg[0][3] == min(x[3] for x in first)
    assert agg[0][5] == sum(x[5] for x in first)
    assert len(it.aggregate_session(b, 5)) == 75


def test_minutes_outside_the_session_are_dropped():
    b = day_minutes(D0)
    pre = [(open_ts(D0) - 60, 100, 101, 99, 100, 5.0)]
    post = [(open_ts(D0) + 375 * 60, 100, 101, 99, 100, 5.0)]
    agg = it.aggregate_session(pre + b + post, 15)
    assert len(agg) == 25 and agg[-1][0] == open_ts(D0) + 24 * 900


def test_partial_bar_is_dropped_only_when_now_is_given():
    b = day_minutes(D0, n=40)
    now = open_ts(D0) + 39 * 60 + 30
    assert len(it.aggregate_session(b, 15, now=now)) == 2
    assert len(it.aggregate_session(b, 15, now=open_ts(D0) + 45 * 60)) == 3
    assert len(it.aggregate_session(b, 15)) == 3


def test_a_bucket_never_straddles_two_days():
    b = day_minutes(D0) + day_minutes(D0 + 1)
    agg = it.aggregate_session(b, 15)
    assert len(agg) == 50 and agg[25][0] == open_ts(D0 + 1)


def test_duplicate_and_bad_minutes_are_ignored():
    b = day_minutes(D0, n=30)
    junk = [(b[0][0], 100, 99, 101, 100, 1.0), ("x", 1, 1, 1, 1, 1), (b[1][0], -1, 1, 1, 1, 1), (b[2][0], float("nan"), 1, 1, 1, 1), (b[3][0],)]
    assert it.aggregate_session(b + junk, 15) == it.aggregate_session(b, 15)


def test_session_info_complete_late_start_and_sparse():
    full = day_minutes(D0)
    late = day_minutes(D0 + 1, start=60, n=315)
    sparse = day_minutes(D0 + 2)[::3]
    info = it.session_info(full + late + sparse)
    assert info[D0]["complete"] is True and info[D0]["coverage"] == 1.0
    assert info[D0 + 1]["complete"] is False and info[D0 + 1]["minutes"] == 315
    assert info[D0 + 2]["complete"] is False and info[D0 + 2]["coverage"] < 0.4


def test_session_vwap_uses_only_the_last_day():
    a, b = day_minutes(D0, seed=1), day_minutes(D0 + 1, seed=2, base=300.0)
    agg = it.aggregate_session(a + b, 15)
    v = it.session_vwap(agg)
    only_b = [x for x in agg if x[0] >= open_ts(D0 + 1)]
    want = sum((x[2] + x[3] + x[4]) / 3 * x[5] for x in only_b) / sum(x[5] for x in only_b)
    assert abs(v - want) < 1e-9 and 250 < v < 350
    assert it.session_vwap([]) is None
    assert it.session_vwap([(open_ts(D0), 1, 1, 1, 1, 0.0)]) is None


def test_params_are_intraday_and_hash_differs_from_daily():
    p15, p5 = it.intraday_params(15), it.intraday_params(5)
    assert p15.min_bars == 50 and p15.full_bars == 100 and p15.gap_event_pct == 3.0 and isinstance(p15.min_bars, int)
    assert p5.min_bars == 75 and p5.objective_pct == 1.0
    assert params_hash(p15) != params_hash(MSParams()) and params_hash(p15) != params_hash(p5)
    assert it.intraday_params(30) == p15
    assert it.intraday_params(15, MSParams(rr_min=2.0)).rr_min == 2.0


def test_analyze_intraday_keys_are_prefixed_and_quality_follows_history():
    out = it.analyze_intraday(days(6), 15)
    assert out["tf_min"] == 15 and out["days"] == 6 and out["bars"] == 150 and out["quality"] in ("ok", "limited")
    s = out["summary"]
    assert "i15_trend" in s and "i15_groups_strict" in s and "i15_hash" in s and not any(k.startswith("ms_") for k in s)
    assert s["i15_hash"] == params_hash(it.intraday_params(15))
    short = it.analyze_intraday(days(1), 15)
    assert short["quality"] == "insufficient" and short["summary"]["i15_trend"] is None


def test_5m_and_15m_both_run_and_do_not_share_prefixes():
    b = days(5)
    a15, a5 = it.analyze_intraday(b, 15)["summary"], it.analyze_intraday(b, 5)["summary"]
    assert set(a15) and set(a5) and not (set(a15) & set(a5))


def test_keep_block_only_when_asked():
    assert "block" not in it.analyze_intraday(days(4), 15)
    assert "block" in it.analyze_intraday(days(4), 15, keep_block=True)


def test_never_raises_on_garbage():
    for bad in (None, [], [(1, 2)], [("a",) * 6], [(float("nan"),) * 6]):
        out = it.analyze_intraday(bad, 15)
        assert out["quality"] in ("unavailable", "insufficient") and "summary" in out


def test_replay_row_equals_a_fresh_analysis_of_the_same_prefix():
    from market_structure.engine import analyze
    from market_structure.summary import flatten_summary
    agg = it.aggregate_session(days(6, seed=4), 15)
    rows = it.replay_intraday(agg, 15, step=17)
    assert len(rows) >= 3
    for r in rows:
        cut = agg[: r["i"] + 1]
        want = it.prefix_summary(flatten_summary(analyze(cut, params=it.intraday_params(15), ctx=it.intraday_ctx(cut))), 15)
        assert r["summary"] == want, r["i"]


def test_appending_later_bars_never_changes_an_earlier_replay_row():
    agg = it.aggregate_session(days(7, seed=9), 15)
    short = it.replay_intraday(agg[:120], 15, step=1)
    long = it.replay_intraday(agg, 15, step=1)
    by_i = {r["i"]: r["summary"] for r in long}
    assert short and all(by_i[r["i"]] == r["summary"] for r in short)


def test_alignment():
    assert it.alignment("bullish", "bullish") == "aligned_bull" and it.alignment("bearish", "bearish") == "aligned_bear"
    assert it.alignment("bullish", "bearish") == "conflict"
    assert it.alignment("bullish", None) is None and it.alignment("neutral", "bullish") is None
