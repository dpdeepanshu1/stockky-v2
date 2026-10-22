"""GENERATED COPY of shared/market_structure/tests/test_chart_context_intraday.py (imports the vendored stockky_ms) so the service coverage gate sees it.
Edit the shared file, then re-copy (sed s/market_structure/stockky_ms/)."""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockky_ms import chart_context as CC
from stockky_ms import timeframes as TF
from stockky_ms.chart_context import chart_context
from stockky_ms.ms_types import IST_OFFSET_S

DAYS = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
        "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]


def bars_for(tf, n_days, seed=3, drift=0.02):
    import datetime as _dt
    mins = TF.spec(tf)["minutes"]
    per = -(-375 // mins)
    rnd, px, out = random.Random(seed), 100.0, []
    for day in DAYS[-n_days:]:
        d0 = _dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp() - IST_OFFSET_S + 9 * 3600 + 15 * 60
        for k in range(per):
            o, c = px, max(5.0, px + rnd.gauss(drift, 0.35))
            out.append((d0 + k * mins * 60, o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1000.0 + k))
            px = c
    return out, [TF.label_for(b[0], tf) for b in out]


def test_labels_keep_the_time_and_the_payload_says_which_timeframe():
    bars, ds = bars_for("15m", 10)
    out = chart_context(bars, ds, tail=120, tf="15m")
    assert out["ok"] and out["tf"] == "15m" and out["unit"] == "candle" and out["note"] is None and out["errors"] == []
    assert all(len(b[0]) == 16 and b[0][10] == " " for b in out["bars"]) and out["bars"][-1][0] == ds[-1] == out["last_bar"]
    assert len({b[0] for b in out["bars"]}) == len(out["bars"])


def test_noisy_timeframes_carry_a_plain_caution():
    for tf in ("1m", "5m"):
        bars, ds = bars_for(tf, 3)
        out = chart_context(bars, ds, tail=200, tf=tf)
        assert out["ok"] and out["note"] and "noisy" in out["note"]


def test_every_timeframe_runs_and_the_daily_default_is_unchanged():
    for tf, nd in (("1m", 3), ("5m", 6), ("15m", 10), ("30m", 12), ("1h", 14)):
        bars, ds = bars_for(tf, nd)
        out = chart_context(bars, ds, tail=150, tf=tf)
        assert out["ok"] and out["tf"] == tf and out["errors"] == [] and 30 <= len(out["bars"]) <= 150, tf
    bars, ds = bars_for("15m", 10)
    d = chart_context(bars, [x[:10] for x in ds])
    assert d["tf"] == "1d" and d["unit"] == "day" and d["live"] is None and all(len(b[0]) == 10 for b in d["bars"])
    assert chart_context(bars, ds, tf="7m")["tf"] == "1d"


def test_wording_says_candle_not_day_on_intraday():
    bars, ds = bars_for("15m", 14)
    out = chart_context(bars, ds, tail=200, tf="15m", structure_trend="bullish")
    tr = [b for b in out["badges"] if b["key"] == "regime_trend"]
    assert tr and "-candle" in tr[0]["tip"] and "-day" not in tr[0]["tip"]
    assert [b for b in out["badges"] if b["key"] == "regime_structure"][0]["label"] == "Daily structure: up"
    d = chart_context(bars, [x[:10] for x in ds], structure_trend="bullish")
    assert [b for b in d["badges"] if b["key"] == "regime_structure"][0]["label"] == "Structure: up"


def test_candle_badge_says_on_the_latest_closed_candle():
    open0 = TF._open_ts(TF._day_no(1_700_000_000))
    lead = [(open0 + i * 300, 160.0 - i, 160.5 - i, 159.5 - i, 159.7 - i, 1000.0) for i in range(60)]
    bars = lead + [(lead[-1][0] + 300, 100.0, 100.5, 98.0, 100.4, 1000.0)]
    out = chart_context(bars, [TF.label_for(b[0], "5m") for b in bars], tf="5m")
    hm = [m for m in out["markers"] if m["name"] == "hammer"]
    assert hm and hm[0]["date"] == out["last_bar"] and hm[0]["bars_ago"] == 0
    b = [x for x in out["badges"] if x["key"] == "hammer"][0]
    assert "latest closed candle" in b["tip"] and "today" not in b["tip"]


def _canned(name):
    return lambda b, **kw: {"candles": [{"name": name, "bias": "bullish", "bars_ago": 0}], "chart": [], "errors": []}


def test_a_two_or_three_candle_pattern_over_a_session_break_is_not_shown_on_intraday_but_is_on_daily():
    bars, ds = bars_for("15m", 6)
    fod = next(i for i in range(1, len(bars)) if TF._day_no(bars[i][0]) != TF._day_no(bars[i - 1][0]))
    real = CC.analyze_patterns
    names = lambda cut, tf="15m": [m["name"] for m in chart_context(bars[:cut], [x[:10] for x in ds[:cut]] if tf == "1d" else ds[:cut], tf=tf)["markers"]]
    try:
        CC.analyze_patterns = _canned("bullish_engulfing")
        assert names(fod + 1) == []
        assert names(fod + 5) == ["bullish_engulfing"]
        CC.analyze_patterns = _canned("hammer")
        assert names(fod + 1) == ["hammer"]
        CC.analyze_patterns = _canned("morning_star")
        assert names(fod + 2) == []
        assert names(fod + 3) == ["morning_star"]
        assert names(fod + 2, "1d") == ["morning_star"]
    finally:
        CC.analyze_patterns = real


def test_live_bar_and_lag_pass_through_and_bad_live_is_dropped():
    bars, ds = bars_for("5m", 4)
    t = bars[-1][0] + 300
    live = (t, 101.0, 101.8, 100.7, 101.5, 321.0)
    out = chart_context(bars, ds, tf="5m", live=live, live_date=TF.label_for(t, "5m"), lag_s=95.4)
    assert out["live"] == [TF.label_for(t, "5m"), 101.0, 101.8, 100.7, 101.5, 321.0] and out["data_lag_s"] == 95
    assert all(b[0] != out["live"][0] for b in out["bars"])
    for bad in (None, (t, float("nan"), 1, 1, 1, 1), (t, 1, 2), "x"):
        assert chart_context(bars, ds, tf="5m", live=bad, live_date=TF.label_for(t, "5m"))["live"] is None
    assert chart_context(bars, ds, tf="5m", live=live, live_date=None)["live"] is None
    assert chart_context(bars, ds, tf="5m", lag_s=None)["data_lag_s"] is None and chart_context(bars, ds, tf="5m", lag_s="x")["data_lag_s"] is None


def test_pattern_engine_only_sees_closed_bars():
    bars, ds = bars_for("5m", 4)
    seen = {}
    real = CC.analyze_patterns
    try:
        def spy(b, **kw):
            seen["n"] = len(b)
            return real(b, **kw)
        CC.analyze_patterns = spy
        t = bars[-1][0] + 300
        chart_context(bars, ds, tf="5m", live=(t, 1, 2, 1, 2, 5), live_date=TF.label_for(t, "5m"))
        assert seen["n"] == len(bars)
    finally:
        CC.analyze_patterns = real


def test_intraday_short_series_degrades_without_raising():
    bars, ds = bars_for("1h", 1)
    assert chart_context(bars[:3], ds[:3], tf="1h")["ok"] is False
    assert chart_context(None, None, tf="1m")["ok"] is False and chart_context(bars, ds[:-2], tf="1h")["ok"] in (True, False)
