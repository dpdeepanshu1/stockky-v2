"""WS-6: observation rows, due(), evaluate_row() and aggregate() - including that rejected stocks are tracked like the passed ones."""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import DAY, T0
from market_structure.candles import candles_to_bars, day_key, ist_date
from market_structure.observation import BENCH_FIELDS, OUTCOME_FIELDS, aggregate, bars_after, bench_outcome, classify, due, evaluate_row, new_observation

OBS_TS = 1_700_000_000 + 4 * 3600          # a trading-hours stamp


def _row(verdict="passed", **kw):
    return new_observation("tcs", "real_trade", OBS_TS, 100.0, verdict, stop=95.0, target=110.0, atr=2.0, mode="real", gate_mode="enforce",
                           reasons=["chase 2.0 ATR"], summary={"ms_tqs": 61, "ms_trend": "bullish", "junk": 1}, **kw)


def test_classify():
    assert classify("no_opinion", True, True) == "no_opinion"
    assert classify("ok", False, False) == "passed"
    assert classify("ok", True, False) == "rejected_enforced"   # no record-only mode any more
    assert classify("ok", True, True) == "rejected_enforced"


def test_new_observation_shape_and_guards():
    r = _row("rejected_enforced")
    assert r["symbol"] == "TCS" and r["mode"] == "REAL" and r["obs_day"] == ist_date(OBS_TS) and r["complete"] is False and r["bars_seen"] == 0
    assert json.loads(r["reasons_json"]) == ["chase 2.0 ATR"] and json.loads(r["summary_json"]) == {"ms_tqs": 61, "ms_trend": "bullish"}
    assert all(r[k] is None for k in OUTCOME_FIELDS if k not in ("bars_seen", "complete"))
    assert new_observation("", "x", OBS_TS, 100, "passed") is None
    assert new_observation("A", "x", OBS_TS, 0, "passed") is None
    assert new_observation("A", "x", OBS_TS, float("nan"), "passed") is None
    assert new_observation("A", "x", OBS_TS, 100, "bogus") is None


def test_due_waits_for_a_later_session_and_stops_when_complete():
    r = _row()
    assert due(r, OBS_TS + 3600)["due"] is False and due(r, OBS_TS + 3600)["reason"] == "same_day"
    d = due(r, OBS_TS + 2 * DAY)
    assert d["due"] is True and 10 < d["lookback_days"] <= 120
    r["complete"] = True
    assert due(r, OBS_TS + 40 * DAY) == {"due": False, "reason": "complete", "lookback_days": 0}
    assert due(_row(), OBS_TS + 900 * DAY)["lookback_days"] == 120


def _bars_from(obs_ts, closes):
    k = day_key(obs_ts)
    base = (k - 1) * DAY                       # midnight UTC of the observation's IST date minus one day...
    return [(float((k + 1 + i) * DAY), c, c + 1, c - 1, c, 1000.0) for i, c in enumerate(closes)]


def test_bars_after_excludes_the_observation_day_and_earlier():
    k = day_key(OBS_TS)
    bars = [(float((k + d) * DAY), 100, 101, 99, 100, 1) for d in (-2, -1, 0, 1, 2)]
    got = bars_after(bars, OBS_TS)
    assert [day_key(b[0]) for b in got] == [k + 1, k + 2]


def test_evaluate_row_fills_outcome_fields_only():
    r = _row()
    out = evaluate_row(r, _bars_from(OBS_TS, [101 + i for i in range(25)]))
    assert set(out) == set(OUTCOME_FIELDS) and out["complete"] is True and out["ret_1d"] == 1.0 and out["ret_20d"] == 20.0
    r.update(out)
    assert due(r, OBS_TS + 60 * DAY)["due"] is False
    part = evaluate_row(_row(), _bars_from(OBS_TS, [101, 102, 103]))
    assert part["bars_seen"] == 3 and part["complete"] is False and part["ret_5d"] is None


def test_candles_to_bars_cleans_and_orders():
    cs = [{"date": "2026-10-02", "open": 11, "high": 12, "low": 10, "close": 11.5, "volume": 5}, {"date": "bad", "close": 1, "high": 1, "low": 1},
          {"date": "2026-10-01", "open": None, "high": 12, "low": 10, "close": 11}, {"date": "2026-10-03", "close": None, "high": 1, "low": 1}, "x"]
    b = candles_to_bars(cs)
    assert len(b) == 2 and b[0][0] < b[1][0] and b[0][1] == 11.0 and ist_date(b[0][0]) == "2026-10-01"


def _rows(verdict, rets, h="ret_10d"):
    out = []
    for x in rets:
        r = _row("rejected_enforced" if verdict == "rejected_shadow" else verdict)
        r["verdict"] = verdict          # "rejected_shadow" = a legacy row written before group370; still read as rejected
        r.update({h: x, "bars_seen": 10, "complete": False, "mfe_pct": abs(x), "mae_pct": -abs(x) / 2, "first_touch": "target" if x > 0 else "stop"})
        out.append(r)
    return out


def test_aggregate_edge_and_conclusion():
    rows = _rows("passed", [2.0] * 25) + _rows("rejected_shadow", [-2.0] * 15) + _rows("rejected_enforced", [-2.0] * 10) + _rows("no_opinion", [0.5] * 5)
    a = aggregate(rows)
    assert a["total"] == 55 and a["by_verdict"]["rejected"]["n"] == 25 and a["by_verdict"]["passed"]["horizons"]["10d"]["mean"] == 2.0
    assert a["edge"]["10d"]["edge"] == 4.0 and a["conclusion"] == "gate_helps" and a["conclusion_horizon"] == "10d"
    assert a["by_verdict"]["passed"]["target_first"] == 25 and a["by_verdict"]["rejected"]["stop_first"] == 25
    bad = aggregate(_rows("passed", [-1.0] * 25) + _rows("rejected_shadow", [3.0] * 25))
    assert bad["conclusion"] == "gate_hurts"


def test_aggregate_small_or_empty_samples_do_not_conclude():
    assert aggregate([])["conclusion"] == "insufficient_sample"
    few = aggregate(_rows("passed", [2.0] * 3) + _rows("rejected_shadow", [-2.0] * 3))
    assert few["conclusion"] == "insufficient_sample" and few["edge"]["10d"]["edge"] == 4.0
    assert aggregate([{"verdict": "passed"}, None, "x"])["total"] == 1


# ---- group402: market-relative returns ------------------------------------------------------------------------------------------------
def _bench_bars(closes, start=0):
    """Daily bars from the observation day (start=0) on: bar i closes at closes[i]."""
    return [(OBS_TS + (start + i) * DAY, c, c + 1, c - 1, c, 1.0) for i, c in enumerate(closes)]


def test_bench_outcome_is_measured_from_the_close_on_the_observation_day():
    out = bench_outcome(OBS_TS, _bench_bars([100.0] + [100.0 + i for i in range(1, 26)]))
    assert set(out) == set(BENCH_FIELDS) and out["bench_ret_1d"] == 1.0 and out["bench_ret_5d"] == 5.0 and out["bench_ret_20d"] == 20.0
    part = bench_outcome(OBS_TS, _bench_bars([100.0, 101.0, 102.0]))
    assert part["bench_ret_1d"] == 1.0 and part["bench_ret_5d"] is None and part["bench_ret_20d"] is None


def test_bench_outcome_unknown_is_none_never_zero():
    none = {k: None for k in BENCH_FIELDS}
    assert bench_outcome(OBS_TS, []) == none and bench_outcome(OBS_TS, None) == none and bench_outcome(None, _bench_bars([100.0, 101.0])) == none
    assert bench_outcome(OBS_TS, _bench_bars([101.0, 102.0], start=1)) == none          # no bar ON the observation day -> no base -> unknown
    assert bench_outcome(OBS_TS, _bench_bars([0.0, 101.0])) == none and bench_outcome(OBS_TS, "junk") == none


def test_aggregate_excess_takes_the_market_out_of_the_edge():
    rows = _rows("passed", [6.0] * 25) + _rows("rejected_enforced", [5.0] * 25)
    for r in rows:
        r["bench_ret_10d"] = 5.0
    a = aggregate(rows)
    assert a["edge"]["10d"]["edge"] == 1.0 and a["edge_excess"]["10d"] == {"edge": 1.0, "rejected_n": 25, "passed_n": 25}
    assert a["by_verdict"]["passed"]["excess"]["10d"] == {"n": 25, "mean": 1.0, "win_pct": 100.0}
    assert a["by_verdict"]["rejected"]["excess"]["10d"]["mean"] == 0.0 and a["by_verdict"]["rejected"]["excess"]["10d"]["win_pct"] == 0.0


def test_aggregate_excess_ignores_rows_without_a_benchmark_value():
    rows = _rows("passed", [6.0] * 4) + _rows("rejected_enforced", [5.0] * 4)
    rows[0]["bench_ret_10d"] = 2.0
    a = aggregate(rows)
    assert a["by_verdict"]["passed"]["excess"]["10d"] == {"n": 1, "mean": 4.0, "win_pct": 100.0}
    assert a["edge_excess"] == {} and a["by_verdict"]["passed"]["excess"]["5d"]["mean"] is None
    assert aggregate(_rows("passed", [1.0] * 3))["edge_excess"] == {}
