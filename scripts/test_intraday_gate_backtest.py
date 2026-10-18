"""group441: scripts/intraday_gate_backtest.py, scripts/export_intraday.py and scripts/_intraday_data.py.
The replayed VWAP / opening rules are checked AGAINST the live gates (real-trade-service/entry_engine) so they cannot drift apart.
Plain asserts (python3 scripts/test_intraday_gate_backtest.py) or pytest."""
from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "shared"))

import _intraday_data as data  # noqa: E402
import export_intraday as ex  # noqa: E402
import intraday_gate_backtest as gb  # noqa: E402

TH = dict(gb.DEFAULTS)


def _minutes(date, seed, base=100.0, n=375, start=0, vol=0.0015, drift=0.0):
    rnd = random.Random(f"{date}-{seed}")
    o_ts = data.open_ts(date)
    px, out = base, []
    for m in range(start, start + n):
        o = px
        c = max(1.0, o * (1 + rnd.gauss(drift, vol)))
        out.append([o_ts + m * 60, round(o, 4), round(max(o, c) * 1.0006, 4), round(min(o, c) * 0.9994, 4), round(c, 4), 1000.0 + (m % 30) * 10])
        px = c
    return out


def _write(root, date, sym, bars, backfilled=False):
    d = Path(root) / date
    d.mkdir(parents=True, exist_ok=True)
    (d / (quote(sym, safe="") + ".json")).write_text(json.dumps({"v": 1, "symbol": sym, "date": date, "tf": 1, "first_ts": bars[0][0], "last_ts": bars[-1][0],
                                                              "n": len(bars), "backfilled": backfilled, "bars": bars}))


def _dates(k, start=datetime(2026, 9, 1)):
    out, d = [], start
    while len(out) < k:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _folder(root, syms=("AAA", "BBB", "M&M"), n_days=24, drift=0.0):
    for si, s in enumerate(syms):
        base = 100.0 + 40 * si
        for date in _dates(n_days):
            b = _minutes(date, si, base, drift=drift)
            _write(root, date, s, b)
            base = b[-1][4]


def test_loader_reads_complete_late_and_damaged_files():
    with tempfile.TemporaryDirectory() as td:
        d1, d2, d3 = _dates(3)
        _write(td, d1, "M&M", _minutes(d1, 1))
        _write(td, d2, "M&M", _minutes(d2, 1, start=90, n=285), backfilled=True)
        (Path(td) / d3).mkdir()
        (Path(td) / d3 / "BAD.json").write_text("{not json")
        (Path(td) / "notes").mkdir()
        st = {}
        got = data.load_dir(td, stats=st)
        assert set(got) == {"M&M"} and st["bad_files"] == 1 and st["files"] == 2
        assert got["M&M"][d1]["complete"] is True and got["M&M"][d2]["complete"] is False and got["M&M"][d2]["backfilled"] is True
        assert data.previous_day(got["M&M"], d2) == d1 and data.previous_day(got["M&M"], d1) is None
        assert data.load_dir(td, symbols=["nope"]) == {} and data.load_dir("/does/not/exist") == {}


def test_previous_day_respects_the_six_day_limit():
    days = {"2026-10-01": {}, "2026-10-12": {}}
    assert data.previous_day(days, "2026-10-12") is None                       # 11 calendar days apart
    assert data.previous_day(days, "2026-10-05") == "2026-10-01"


def test_vwap_flag_matches_the_live_semantics():
    assert gb.vwap_flag(99.0, 100.0, 0.15, 15.0) is True
    assert gb.vwap_flag(99.9, 100.0, 0.15, 15.0) is False
    assert gb.vwap_flag(80.0, 100.0, 0.15, 15.0) is False
    assert gb.vwap_flag(99.0, None, 0.15, 15.0) is False and gb.vwap_flag(99.0, 0.0, 0.15, 15.0) is False


def test_opening_flags_each_rule_and_the_settle_time():
    f = lambda **kw: gb.opening_flags(**{"minutes_open": 20.0, "price": 101.0, "prev_close": 100.0, "day_high": 102.0, "day_low": 99.0, "prev_close_pos": 0.7, "th": TH, **kw})  # noqa: E731
    assert f() == []
    assert f(price=99.0) == ["og_below_prev_close"] and f(price=104.0, day_high=110.0, day_low=99.0) == ["og_extended"]
    assert f(price=101.9, day_high=102.0, day_low=100.0) == ["og_near_day_high"]
    assert f(prev_close_pos=0.3) == ["og_prev_weak_close"]
    assert f(minutes_open=5.0, price=101.9, day_high=102.0, day_low=100.0) == []
    assert f(minutes_open=45.0, price=99.0) == [] and f(minutes_open=-1.0, price=99.0) == []
    assert f(day_high=100.0, day_low=100.0) == ["og_near_day_high"]
    assert f(prev_close_pos=None) == []
    assert gb.opening_flags(2.0, 101.0, 100.0, 102.0, 99.0, 0.7, {**TH, "og_min_minutes": 5.0}) == ["og_too_early"]


# parity with the REAL gates; a subprocess keeps the services' own `config` modules apart
_PROBE = r'''
import json, sys, types
from datetime import datetime, timedelta, timezone
sys.path.insert(0, sys.argv[1])
import config
from entry_engine import opening_gate, prev_day, vwap_gate
cases = json.loads(sys.stdin.read())
IST = timezone(timedelta(hours=5, minutes=30))
opening_gate.is_active = lambda mode, now=None: True
out = []
for c in cases:
    for k, v in c["th"].items():
        setattr(config, k, v)
    if c["kind"] == "vwap":
        out.append(vwap_gate.reject_reason(c["price"], c["vwap"]))
        continue
    pos = c["prev_pos"]
    pd = prev_day.PrevDay(high=110.0, low=100.0 if pos is not None else 110.0, close=(100.0 + 10.0 * pos) if pos is not None else 110.0, atr=None, date="2026-10-16")
    prev_day.get = lambda sym, _pd=pd: _pd
    tick = types.SimpleNamespace(price=c["price"], prev_close=c["prev_close"], day_high=c["hi"], day_low=c["lo"], symbol="X", atr=None)
    now = datetime(2026, 10, 19, 9, 15, tzinfo=IST) + timedelta(minutes=c["m"])
    out.append(opening_gate.reject_reason("SCALP", tick, now=now, stop_pct=None))
print(json.dumps(out))
'''
_OG_KEYS = {"OPENING_GATE_MIN_MINUTES_AFTER_OPEN": "og_min_minutes", "OPENING_GATE_RANGE_AFTER_MIN": "og_range_after_min", "OPENING_GATE_MIN_CHANGE_PCT": "og_min_chg",
            "OPENING_GATE_MAX_CHANGE_PCT": "og_max_chg", "OPENING_GATE_MAX_RANGE_POS": "og_max_range_pos", "OPENING_GATE_MIN_PREVDAY_CLOSE_POS": "og_min_prev_close_pos"}
_REASON_TO_RULE = {"TOO_EARLY": "og_too_early", "BELOW_PREV_CLOSE": "og_below_prev_close", "EXTENDED": "og_extended", "NEAR_DAY_HIGH": "og_near_day_high",
                   "NO_DAY_RANGE": "og_near_day_high", "PREV_DAY_WEAK_CLOSE": "og_prev_weak_close"}


def _live(cases):
    svc = ROOT / "services" / "real-trade-service"
    r = subprocess.run([sys.executable, "-c", _PROBE, str(svc)], input=json.dumps(cases), capture_output=True, text=True, cwd=str(svc), timeout=180)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _live_th(th):
    d = {k: float(th[v]) for k, v in _OG_KEYS.items()}
    d["OPENING_GATE_MIN_STOP_ATR_FRAC"] = 0.0                       # the stop-vs-ATR check is not replayed
    d["OPENING_GATE_ENABLED"] = True
    d["ENTRY_VWAP_GATE_ENABLED"] = True
    d["ENTRY_VWAP_TOLERANCE_PCT"] = th["vwap_tol_pct"]
    return d


def test_opening_rules_agree_with_the_live_gate_over_a_grid():
    cases, mine = [], []
    for m in (0.0, 3.0, 14.0, 15.0, 30.0, 44.0):
        for price in (98.0, 99.4, 100.0, 101.5, 102.9, 103.5):
            for hi, lo in ((103.0, 98.0), (101.0, 100.0), (100.0, 100.0)):
                for pos in (0.2, 0.5, 0.9, None):
                    cases.append({"kind": "og", "th": _live_th(TH), "m": m, "price": price, "prev_close": 100.0, "hi": hi, "lo": lo, "prev_pos": pos})
                    mine.append(gb.opening_flags(m, price, 100.0, hi, lo, pos, TH))
    live = _live(cases)
    assert len(live) == len(mine) > 400
    for c, lv, mi in zip(cases, live, mine):
        assert (lv is not None) == bool(mi), (c, lv, mi)
        if lv is not None:
            assert mi[0] == _REASON_TO_RULE[lv.split(":")[1]], (c, lv, mi)


def test_opening_rules_agree_with_the_live_gate_with_overridden_thresholds():
    th = {**TH, "og_min_minutes": 5.0, "og_max_chg": 1.5, "og_max_range_pos": 0.0, "og_min_prev_close_pos": 0.0}
    cases, mine = [], []
    for m in (2.0, 6.0, 20.0):
        for price in (99.0, 100.5, 102.0):
            cases.append({"kind": "og", "th": _live_th(th), "m": m, "price": price, "prev_close": 100.0, "hi": 102.0, "lo": 100.0, "prev_pos": 0.1})
            mine.append(gb.opening_flags(m, price, 100.0, 102.0, 100.0, 0.1, th))
    for c, lv, mi in zip(cases, _live(cases), mine):
        assert (lv is not None) == bool(mi), (c, lv, mi)


def test_vwap_rule_agrees_with_the_live_gate():
    cases, mine = [], []
    for price in (79.0, 84.0, 99.0, 99.84, 99.86, 100.0, 105.0, 120.0):
        for vwap in (None, 0.0, 100.0):
            cases.append({"kind": "vwap", "th": _live_th(TH), "price": price, "vwap": vwap})
            mine.append(gb.vwap_flag(price, vwap, TH["vwap_tol_pct"], TH["vwap_max_dist_pct"]))
    for c, lv, mi in zip(cases, _live(cases), mine):
        assert (lv is not None) == mi, (c, lv, mi)


def test_entry_rows_use_only_finished_minutes_and_the_next_open():
    date, prev = _dates(2)
    b = _minutes(date, 1)
    rows = gb.entry_rows("AAA", date, b, _minutes(prev, 1), TH, every=15, until_min=60, cost_pct=0.0, slip_pct=0.0)
    r = next(x for x in rows if x["m"] == 30)
    o_ts = data.open_ts(date)
    k = int((o_ts + 30 * 60 - b[0][0]) // 60)
    assert b[k][0] == o_ts + 1800
    entry = b[k][1]
    exit_close = b[k + 30 - 1][4]
    assert abs(r["r30"] - round((exit_close / entry - 1) * 100, 3)) < 1e-9
    b2 = [list(x) for x in b]
    for x in b2[k + 40:]:
        x[4] *= 3.0                                                              # change only the FUTURE beyond every horizon of this decision
        x[2] *= 3.0
    r2 = next(x for x in gb.entry_rows("AAA", date, b2, _minutes(prev, 1), TH, every=15, until_min=60, cost_pct=0.0, slip_pct=0.0) if x["m"] == 30)
    assert r2["r30"] == r["r30"] and r2["og"] == r["og"] and r2["rej_vwap"] == r["rej_vwap"]


def test_cost_and_slippage_are_subtracted_and_late_exits_have_no_return():
    date, _ = _dates(2)
    b = _minutes(date, 2)
    free = gb.entry_rows("AAA", date, b, None, TH, every=30, until_min=330, cost_pct=0.0, slip_pct=0.0)
    paid = gb.entry_rows("AAA", date, b, None, TH, every=30, until_min=330, cost_pct=0.12, slip_pct=0.0)
    assert [r["r30"] for r in paid][0] == round(free[0]["r30"] - 0.12, 3)
    last = [r for r in free if r["m"] == 330][0]
    assert last["r60"] is None and last["r30"] is not None                      # 330 + 60 > 370: no 60-minute exit; 330 + 30 = 360 still fits
    mid = [r for r in free if r["m"] == 300][0]
    assert mid["r60"] is not None                                               # 300 + 60 = 360 <= 370
    assert all(r["og"] is None for r in free)                                    # no previous day: the opening gate is not evaluated


def test_stale_and_gappy_minutes_skip_the_entry():
    date, _ = _dates(2)
    b = _minutes(date, 3)
    gap = [x for x in b if not (data.open_ts(date) + 28 * 60 <= x[0] < data.open_ts(date) + 40 * 60)]
    rows = gb.entry_rows("AAA", date, gap, None, TH, every=5, until_min=60, cost_pct=0.0, slip_pct=0.0)
    ms = {r["m"] for r in rows}
    assert 35 not in ms and 40 not in ms and 10 in ms and 55 in ms


def test_partial_and_short_sessions_produce_nothing():
    date, _ = _dates(2)
    assert gb.entry_rows("AAA", date, _minutes(date, 1, n=10), None, TH) == []


def test_collect_skips_late_sessions_and_counts_them():
    with tempfile.TemporaryDirectory() as td:
        ds = _dates(4)
        for date in ds[:3]:
            _write(td, date, "AAA", _minutes(date, 1))
        _write(td, ds[3], "AAA", _minutes(ds[3], 1, start=120, n=255))
        c = gb.collect(td, TH, 15, 300, 0.12, 0.05)
        assert c["symbol_days_used"] == 3 and c["symbol_days_partial_skipped"] == 1 and len(c["sessions"]) == 3
        assert gb.collect(td, TH, 15, 300, 0.12, 0.05, include_partial=True)["symbol_days_used"] == 4


def test_report_is_insufficient_with_few_sessions_and_no_data_when_empty():
    with tempfile.TemporaryDirectory() as td:
        _folder(td, n_days=5)
        rc = gb.main([td, "--min-n", "10", "--boot", "100"])
        rep = json.loads((Path(td) / "gate_out" / "gate_backtest.json").read_text())
        assert rc == 0 and rep["vwap_gate"]["verdict"] == "insufficient_sample" and rep["opening_gate"]["verdict"] == "insufficient_sample"
        assert rep["sessions"]["count"] == 5 and rep["entries_sampled"] > 100 and (Path(td) / "gate_out" / "gate_backtest.md").exists()
    with tempfile.TemporaryDirectory() as td:
        assert gb.main([td]) == 1
        assert json.loads((Path(td) / "gate_out" / "gate_backtest.json").read_text())["vwap_gate"]["verdict"] == "no_data"


def test_with_enough_sessions_a_real_vwap_edge_is_found_and_noise_is_not():
    """Plant an edge (below its recent mean keeps falling) -> rejecting those entries must show a positive edge. Pure noise must not pass."""
    def build(td, planted):
        for si, s in enumerate(("A", "B", "C", "D")):
            for date in _dates(30):
                rnd = random.Random(f"{date}{s}")
                o_ts = data.open_ts(date)
                px, bars = 100.0 + 10 * si, []
                below = 0.0
                for m in range(375):
                    d = rnd.gauss(0.0, 0.0012)
                    if planted and m >= 20:
                        d += -0.0006 if below < -0.0005 else 0.0003
                    o, c = px, px * (1 + d)
                    bars.append([o_ts + m * 60, round(o, 4), round(max(o, c) * 1.0004, 4), round(min(o, c) * 0.9996, 4), round(c, 4), 1000.0])
                    below = (c / (sum(x[4] for x in bars[-60:]) / len(bars[-60:])) - 1.0)
                    px = c
                _write(td, date, s, bars)
    meta = {"every": 5, "until": "14:30", "cost_pct": 0.12, "slip_pct": 0.05}
    with tempfile.TemporaryDirectory() as td:
        build(td, True)
        g = gb.build_report(gb.collect(td, TH, 5, 315, 0.12, 0.05), 30, 100, 20, 200, meta)["vwap_gate"]
        assert g["entries"]["rejected"] > 200 and g["edge_pp"] is not None and g["edge_pp"] > 0 and g["verdict"] in ("pass", "no_edge")
        assert g["rejected"]["mean_net"] < g["candidate"]["mean_net"]
    with tempfile.TemporaryDirectory() as td:
        build(td, False)
        v = gb.build_report(gb.collect(td, TH, 5, 315, 0.12, 0.05), 30, 100, 20, 200, meta)["vwap_gate"]["verdict"]
        assert v in ("no_edge", "hurts", "insufficient_sample")


def test_export_writes_rows_with_forward_returns_coverage_and_no_lookahead():
    with tempfile.TemporaryDirectory() as td:
        _folder(td, syms=("AAA", "M&M"), n_days=6)
        late = _dates(7)[6]
        _write(td, late, "AAA", _minutes(late, 9, start=100, n=275))
        assert ex.main([td, "--tf", "15", "--step", "3"]) == 0
        out = Path(td) / "intraday_out"
        rows = [json.loads(x) for x in (out / "intraday_rows_15m.jsonl").read_text().splitlines()]
        assert rows and {r["sym"] for r in rows} == {"AAA", "M&M"}
        r = rows[len(rows) // 2]
        assert r["summary"]["i15_hash"] and "ms_trend" not in r["summary"] and r["session_complete"] is True and r["tf"] == 15
        assert set(r["fwd"]) == {"r4", "r8", "mfe8", "mae8"}
        cov = json.loads((out / "coverage.json").read_text())
        assert cov["sessions"] == 7 and cov["symbols"] == 2 and cov["days"][late]["complete"] == 0
        assert (out / "coverage.md").exists()
        assert not any(x["date"] == late for x in rows)


def test_forward_returns_stay_inside_the_session_and_use_the_next_open():
    d1, d2 = _dates(2)
    agg = []
    for date in (d1, d2):
        for k in range(25):
            t = data.open_ts(date) + k * 900
            agg.append((t, 100.0 + k, 101.0 + k, 99.0 + k, 100.5 + k, 10.0))
    f = ex.forward(agg, 5)
    assert f["r4"] == round((agg[9][4] / agg[6][1] - 1) * 100, 3) and f["r8"] == round((agg[13][4] / agg[6][1] - 1) * 100, 3)
    end = ex.forward(agg, 21)
    assert end["r4"] is None and end["r8"] is None
    assert ex.forward(agg, len(agg) - 1)["r4"] is None


def test_export_rejects_an_unsupported_timeframe_and_an_empty_folder():
    with tempfile.TemporaryDirectory() as td:
        assert ex.main([td, "--tf", "30"]) == 2
        assert ex.main([td, "--tf", "15"]) == 1


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
