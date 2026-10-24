"""E111: the Time Machine (timemachine/engine.py, data.py, service.py + the three routes and four switches). Stdlib only: no network, no database, no broker.
Plain functions with plain asserts, so it runs under pytest and under a bare loop."""
import ast
import pathlib
import random
import re
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from timemachine import data as tm_data  # noqa: E402
from timemachine import engine as E  # noqa: E402
from timemachine import service as tm  # noqa: E402

FLAT = E.hhmm("15:00")


# ------------------------------------------------------------------ builders
def cand(day, mod, o, h, l, c):
    return {"date": f"{day} {E.fmt(mod)}", "open": o, "high": h, "low": l, "close": c, "volume": 1000}


def walk_session(seed, sd=0.002, day="2026-09-01"):
    """75 valid 5-minute candle rows: a random walk."""
    r = random.Random(seed)
    px, rows = 100.0, []
    for s in range(75):
        o = px
        c = o * (1 + r.gauss(0, sd))
        h = max(o, c) * (1 + abs(r.gauss(0, sd / 2)))
        l = min(o, c) * (1 - abs(r.gauss(0, sd / 2)))
        rows.append(cand(day, 555 + 5 * s, o, h, l, c))
        px = c
    return rows


def many_days(n=24, sd=0.002):
    rows = []
    for d in range(1, n + 1):
        rows += walk_session(d, sd, day=f"2026-09-{d:02d}")
    return rows


def bars_from(rows):
    return E.complete_sessions(E.parse_candles(rows))


def fake_charges(entry, exit_, qty, short):
    """Flat 20 rupees plus 0.02% of turnover: simple, and the same for both sides."""
    return 20.0 + 0.0002 * (entry + exit_) * qty


def flat_bars(prices, start=555):
    return [(start + 5 * i, o, h, l, c) for i, (o, h, l, c) in enumerate(prices)]


def rule(side="LONG", stop=1.0, target=2.0, slip=0.0, ch=None):
    return E.Rule(side, stop, target, FLAT, slip, ch)


def raises(fn, exc, code=None, status=None):
    try:
        fn()
    except exc as e:
        if code is not None:
            assert e.code == code, (e.code, code)
        if status is not None:
            assert e.status == status, (e.status, status)
        return e
    raise AssertionError("did not raise")


# ------------------------------------------------------------------ helpers
def test_time_helpers():
    assert E.hhmm("09:15") == 555 and E.fmt(555) == "09:15" and E.fmt(E.hhmm("14:55")) == "14:55"
    for bad in ("9", "25:00", "10:61", "ab:cd", ""):
        raises(lambda b=bad: E.hhmm(b), ValueError)
    assert E.N_SLOTS == 75 and E.slot_of(555) == 0 and E.slot_of(925) == 74


def test_wilson_and_quantile():
    assert E.wilson(0, 0) == (None, None)
    lo, hi = E.wilson(50, 100)
    assert 0.40 < lo < 0.5 < hi < 0.60
    lo0, hi0 = E.wilson(0, 10)
    assert lo0 == 0.0 and 0 < hi0 < 0.4
    assert E.quantile([], 0.5) is None and E.quantile([1, 2, 3], 0.0) == 1 and E.quantile([1, 2, 3], 1.0) == 3 and E.quantile([1, 2, 3], 0.5) == 2


# ------------------------------------------------------------------ parsing
def test_parse_candles_drops_bad_rows_and_off_grid_times():
    day = "2026-09-01"
    rows = [cand(day, 555, 100, 101, 99, 100.5), cand(day, 560, 100, 101, 99, 100.5),
            cand(day, 557, 100, 101, 99, 100.5), cand(day, 540, 100, 101, 99, 100.5), cand(day, 930, 100, 101, 99, 100.5),
            cand(day, 565, 100, 99, 101, 100.5), cand(day, 570, 0, 1, 1, 1), cand(day, 575, float("nan"), 1, 1, 1),
            {"date": "garbage"}, {"open": 1}, None]
    out = E.parse_candles(rows)
    assert list(out) == [day] and [b[0] for b in out[day]] == [555, 560]


def test_complete_sessions_keeps_only_full_days():
    rows = many_days(3) + [cand("2026-09-30", 555 + 5 * i, 100, 101, 99, 100) for i in range(10)]
    days = E.parse_candles(rows)
    assert len(days) == 4 and sorted(E.complete_sessions(days)) == ["2026-09-01", "2026-09-02", "2026-09-03"]


# ------------------------------------------------------------------ rule and walk
def test_rule_validation_and_breakeven():
    for args in (("SIDEWAYS", 1.0, 2.0), ("LONG", 0, 2.0), ("LONG", 1.0, -1)):
        raises(lambda a=args: E.Rule(a[0], a[1], a[2], FLAT), ValueError)
    r = rule()
    assert abs(r.be_win() - 1 / 3) < 1e-9 and abs(r.be_win_net() - 1 / 3) < 1e-9
    rc = rule(slip=0.05, ch={"target": 0.1, "stop": 0.1, "flat": 0.1})
    assert rc.be_win_net() > rc.be_win(), "charges and slippage raise the win rate needed"
    assert rc.opposite().side == "SHORT" and rule("SHORT").opposite().side == "LONG" and rc.opposite().charges == rc.charges


def test_result_slippage_only_on_market_legs():
    r = rule(slip=0.05, ch={"target": 0.1, "stop": 0.2, "flat": 0.3})
    g, n, rr = r.result("target", 2.0)
    assert abs(n - (2.0 - 0.05 - 0.1)) < 1e-9 and abs(rr - n) < 1e-9
    assert abs(r.result("stop", -1.0)[1] - (-1.0 - 0.10 - 0.2)) < 1e-9
    assert abs(r.result("flat", 0.4)[1] - (0.4 - 0.10 - 0.3)) < 1e-9


def test_walk_long_target_stop_flat_and_same_bar_is_stop():
    r = rule()
    up = flat_bars([(100, 100.5, 99.9, 100.4), (100.4, 102.3, 100.3, 102.2)])
    assert E.walk(up, 0, r) == ("target", 560, 2.0)
    down = flat_bars([(100, 100.2, 98.9, 99.0), (99, 99.5, 98, 98.5)])
    assert E.walk(down, 0, r) == ("stop", 555, -1.0)
    assert E.walk(flat_bars([(100, 102.5, 98.5, 100)]), 0, r)[0] == "stop", "a bar touching both counts as the stop"
    out = E.walk(flat_bars([(100, 100.3, 99.8, 100.1)] * 3), 0, r)
    assert out[0] == "flat" and out[1] == 565 and abs(out[2] - 0.1) < 1e-6


def test_walk_short_is_the_mirror():
    r = rule("SHORT")
    assert E.walk(flat_bars([(100, 100.2, 97.5, 97.8)]), 0, r) == ("target", 555, 2.0)
    assert E.walk(flat_bars([(100, 101.2, 99.9, 101.0)]), 0, r) == ("stop", 555, -1.0)
    assert E.walk(flat_bars([(100, 102.5, 97.5, 100)]), 0, r)[0] == "stop"
    out = E.walk(flat_bars([(100, 100.2, 99.8, 99.9)] * 2), 0, r)
    assert out[0] == "flat" and abs(out[2] - 0.1) < 1e-6, "a short gains when the close is below the entry"


def test_walk_respects_flat_by_and_entry_after_it():
    bars = [(E.hhmm("14:50"), 100, 100.1, 99.9, 100.0), (E.hhmm("14:55"), 100, 100.1, 99.9, 100.0), (FLAT, 100, 105, 95, 100)]
    out = E.walk(bars, 0, rule())
    assert out[0] == "flat" and out[1] == E.hhmm("14:55"), "the bar at the flat-by time is not walked"
    assert E.walk(bars, 2, rule()) is None, "an entry at the flat-by time is not tested"


# ------------------------------------------------------------------ history
def test_replay_history_rows_and_summary():
    sessions = bars_from(many_days(12))
    r = rule(slip=0.05, ch={"target": 0.1, "stop": 0.1, "flat": 0.1})
    rows = E.replay_history(sessions, r, [E.hhmm("10:00"), E.hhmm("11:00")])
    assert len(rows) == 24 and {x["outcome"] for x in rows} <= {"target", "stop", "flat"}
    for x in rows:
        assert abs(x["r_net"] - (x["gross_pct"] - (0.05 if x["outcome"] == "target" else 0.10) - 0.1)) < 1e-3
    s = E.summarize_rows(rows, r)
    assert s["n"] == 24 and s["target"] + s["stop"] + s["flat"] == 24 and s["sessions"] == 12
    assert s["ci_low"] <= s["p_target"] <= s["ci_high"]
    assert E.summarize_rows([], r) == {"n": 0, "sessions": 0}


# ------------------------------------------------------------------ simulation
def test_synth_sessions_are_valid_and_deterministic():
    pools = E.build_pools(bars_from(many_days(15)))
    assert len(pools) == 75 and all(pools)
    a, b, c = (E.synth_session(pools, random.Random(x)) for x in (5, 5, 6))
    assert a == b and a != c and len(a) == 75
    for m, o, h, l, cl in a:
        assert h >= max(o, cl) - 1e-9 and l <= min(o, cl) + 1e-9 and l > 0
    assert E.synth_session([[] for _ in range(75)], random.Random(1)) == [], "no shapes at all: no session, never a guess"


def test_pools_fill_a_missing_slot_from_its_neighbour():
    rows = [x for x in many_days(12) if x["date"][11:16] != "10:00"]
    pools = E.build_pools(E.parse_candles(rows))
    assert pools[E.slot_of(E.hhmm("10:00"))] and len(E.build_pools({})[0]) == 0


def test_simulate_is_deterministic_and_counts_add_up():
    sessions = bars_from(many_days(15))
    r = rule()
    em = E.hhmm("10:30")
    a, b, c = (E.simulate(sessions, [(r, [em])], em, 2000, sd) for sd in (11, 11, 12))
    sa, sb, sc = (E.summarize_acc(x["per_plan"][0][em], r) for x in (a, b, c))
    assert sa == sb and sa["n"] == 2000 and sa["target"] + sa["stop"] + sa["flat"] == 2000
    assert (sa["target"], sa["stop"]) != (sc["target"], sc["stop"])
    assert len(a["per_plan"][0][em].r_list) == 2000


def test_simulate_rising_pool_long_always_hits_target_short_always_stops():
    rising = [cand("2026-09-%02d" % d, 555 + 5 * i, 100 * 1.006 ** i, 100 * 1.006 ** (i + 1) * 1.0005, 100 * 1.006 ** i * 0.9999, 100 * 1.006 ** (i + 1))
              for d in range(1, 13) for i in range(75)]
    sessions = bars_from(rising)
    long_r, short_r = rule("LONG"), rule("SHORT")
    em = E.hhmm("10:00")
    res = E.simulate(sessions, [(long_r, [em]), (short_r, [em])], em, 300, 3)
    lo = E.summarize_acc(res["per_plan"][0][em], long_r)
    sh = E.summarize_acc(res["per_plan"][1][em], short_r)
    assert lo["target"] == lo["n"] == 300, lo
    assert sh["stop"] == sh["n"] == 300, sh
    assert E.window_drift_pct(sessions) > 20


def test_simulate_dead_flat_pool_is_all_flat():
    still = [cand("2026-09-%02d" % d, 555 + 5 * i, 100, 100, 100, 100) for d in range(1, 13) for i in range(75)]
    r = rule()
    em = E.hhmm("11:00")
    s = E.summarize_acc(E.simulate(bars_from(still), [(r, [em])], em, 200, 1)["per_plan"][0][em], r)
    assert s["flat"] == 200 and s["target"] == 0 and s["stop"] == 0 and s["avg_r_gross"] == 0


def test_simulate_skips_entry_at_or_after_flat_by():
    r = rule()
    late = E.hhmm("15:05")
    res = E.simulate(bars_from(many_days(12)), [(r, [late])], late, 50, 1)
    assert E.summarize_acc(res["per_plan"][0][late], r) == {"n": 0}


def test_r_histogram_covers_everything():
    r = rule()
    rs = [-1.0, -1.0, 2.0, 0.3, -0.4, 1.1]
    h = E.r_histogram(rs, r)
    assert sum(b["n"] for b in h) == len(rs) and abs(sum(b["share"] for b in h) - 1.0) < 1e-9
    assert h[0]["lo"] <= -1.0 and h[-1]["hi"] >= 2.0 and E.r_histogram([], r) == []


# ------------------------------------------------------------------ verdict
def _sim(avg, p=0.34):
    return {"n": 5000, "avg_r_net": avg, "p_target": p}


def _hist(avg, n=20, t=7):
    return {"n": n, "avg_r_net": avg, "p_target": t / max(n, 1), "target": t}


def test_verdict_codes():
    r = rule()
    assert E.verdict(_sim(0.2), _hist(0.3), r, 20)["code"] == "FAVOURS"
    assert E.verdict(_sim(-0.2), _hist(-0.3), r, 20)["code"] == "AGAINST"
    assert E.verdict(_sim(0.01), _hist(0.02), r, 20)["code"] == "NEAR_BREAKEVEN"
    assert E.verdict(_sim(0.2), _hist(-0.3), r, 20)["code"] == "MIXED"
    assert E.verdict(_sim(0.01), _hist(0.5), r, 20)["code"] == "MIXED"
    ned = E.verdict(_sim(0.2), _hist(0.3), r, 5)
    assert ned["code"] == "NOT_ENOUGH_DATA" and "5 complete real sessions" in ned["line"]
    assert E.verdict({"n": 0}, _hist(0.3), r, 30)["code"] == "NOT_ENOUGH_DATA"
    short_hist = E.verdict(_sim(0.2), _hist(-0.9, n=6, t=1), r, 30)
    assert short_hist["code"] == "FAVOURS" and "does not vote" in short_hist["line"]
    v = E.verdict(_sim(0.2), _hist(0.3), r, 20)
    assert "not a forecast" in v["line"] and "needed after charges" in v["line"]


# ------------------------------------------------------------------ data module
def test_normalize_symbol():
    assert tm_data.normalize_symbol(" reliance.ns ") == "RELIANCE" and tm_data.normalize_symbol("M&M") == "M&M" and tm_data.normalize_symbol("bajaj-auto") == "BAJAJ-AUTO"
    for bad in ("", "a b", "x" * 30, "../etc", None, "RELI;ANCE"):
        raises(lambda b=bad: tm_data.normalize_symbol(b), tm_data.DataError, "BAD_SYMBOL", 400)


def test_load_sessions_caches_expires_and_falls_back():
    tm_data.clear_cache()
    calls = []

    def fetch(sym, lb):
        calls.append((sym, lb))
        return many_days(12)
    clock = [1000.0]
    days, info = tm_data.load_sessions("tcs", "1mo", fetch, now=lambda: clock[0])
    assert len(days) == 12 and info["cached"] is False and info["lookback_used"] == "1mo" and calls == [("TCS", "1mo")]
    _d, info2 = tm_data.load_sessions("TCS", "1mo", fetch, now=lambda: clock[0] + 5)
    assert info2["cached"] is True and len(calls) == 1
    clock[0] += tm_data.CACHE_TTL_S + 1
    tm_data.load_sessions("TCS", "1mo", fetch, now=lambda: clock[0])
    assert len(calls) == 2, "an old entry is fetched again"

    tm_data.clear_cache()
    seen = []

    def fetch2(sym, lb):
        seen.append(lb)
        if lb == "2mo":
            raise tm_data.DataError("DATA_ERROR", "no window", 502)
        return many_days(11)
    d2, i2 = tm_data.load_sessions("INFY", "2mo", fetch2)
    assert seen == ["2mo", "1mo"] and i2["fallback"] is True and i2["lookback_used"] == "1mo" and len(d2) == 11


def test_load_sessions_errors_are_raised_not_guessed():
    tm_data.clear_cache()
    for rows in ([], [cand("2026-09-01", 555, 100, 101, 99, 100)]):
        raises(lambda r=rows: tm_data.load_sessions("ITC", "1mo", lambda s, lb: r), tm_data.DataError, "NO_BARS")

    def nosym(s, lb):
        raise tm_data.DataError("NO_SUCH_SYMBOL", "x", 404)
    raises(lambda: tm_data.load_sessions("ZZZZ", "2mo", nosym), tm_data.DataError, "NO_SUCH_SYMBOL", 404)
    assert tm_data.cache_stats()["failures"] >= 3


# ------------------------------------------------------------------ service
def good_fetch(sym, lb):
    return many_days(24)


def test_parse_params_defaults_and_aliases():
    p = tm.parse_params({"symbol": "x"})
    assert p["side"] == "LONG" and p["stop_pct"] == 1.0 and p["target_pct"] == 2.0 and p["entry"] == E.hhmm("10:00") and p["flat_by"] == E.hhmm(config.EOD_SQUAREOFF_TIME_IST)
    assert p["entry"] in p["entry_times"] and p["entry_times"] == sorted(p["entry_times"]) and p["sims"] == config.TIME_MACHINE_SIMS
    s = tm.parse_params({"side": "sell"})
    assert s["side"] == "SHORT" and s["flat_by"] == E.hhmm(config.SHORT_SQUAREOFF_TIME_IST)
    assert tm.parse_params({"side": "buy"})["side"] == "LONG"
    q = tm.parse_params({"entry_time": "11:15", "entry_times": ["10:00", "10:03", "16:00"]})
    assert q["entry_times"] == [E.hhmm("10:00"), E.hhmm("11:15")], "off-grid and after-flat-by times drop out; the chosen entry is always kept"


def test_parse_params_rejects_bad_input():
    bad = [{"side": "up"}, {"stop_pct": 0}, {"stop_pct": 11}, {"target_pct": "x"}, {"target_pct": float("nan")}, {"entry_time": "09:00"}, {"entry_time": "10:07"},
           {"entry_time": "15:00"}, {"flat_by": "09:20"}, {"flat_by": "16:00"}, {"sims": 10}, {"sims": 999999}, {"slippage_pct": -1}, {"value_rs": 10},
           {"lookback": "1y"}, {"entry_times": "10:00"}, {"entry_times": ["x"]}, {"entry_time": "bad"}]
    for b in bad:
        raises(lambda x=b: tm.parse_params(x), tm.TimeMachineError, "BAD_PARAM", 400)
    raises(lambda: tm.parse_params([1]), tm.TimeMachineError, "BAD_PARAM")


def test_make_rule_charges_from_the_charge_model():
    p = tm.parse_params({"value_rs": 20000, "slippage_pct": 0.05})
    r = tm.make_rule(p, 500.0, fake_charges)
    qty = 40
    exp_flat = (20.0 + 0.0002 * (500.0 + 500.0) * qty) / (500.0 * qty) * 100
    assert abs(r.charges["flat"] - exp_flat) < 1e-9 and r.charges["target"] > r.charges["flat"] > 0 and r.slippage_pct == 0.05
    for price in (0, None, -1):
        raises(lambda x=price: tm.make_rule(p, x, fake_charges), tm.TimeMachineError, "NO_PRICE")
    e = raises(lambda: tm.make_rule(tm.parse_params({"value_rs": 1000}), 5000.0, fake_charges), tm.TimeMachineError, "BAD_PARAM")
    assert "below one share" in e.message


def test_run_full_answer_shape_and_consistency():
    tm_data.clear_cache()
    out = tm.run({"symbol": "reliance", "side": "LONG", "stop_pct": 1, "target_pct": 2, "entry_time": "10:30", "sims": 2000, "seed": 4}, fetch=good_fetch, charges_fn=fake_charges)
    assert out["ok"] and out["symbol"] == "RELIANCE" and out["params"]["entry_time"] == "10:30" and out["params"]["rr"] == 2.0
    assert out["data"]["sessions"] == 24 and out["data"]["first_day"] == "2026-09-01" and out["data"]["last_day"] == "2026-09-24"
    sim, hist = out["simulation"], out["history"]
    assert sim["n"] == 2000 and sim["target"] + sim["stop"] + sim["flat"] == 2000 and hist["n"] == 24 and len(out["history_rows"]) == 24
    assert out["breakeven"]["win_net"] > out["breakeven"]["win_gross"] > 0.33
    assert out["control"]["side"] == "SHORT" and out["control"]["simulation"]["n"] == 2000
    assert out["verdict"]["code"] in ("FAVOURS", "AGAINST", "NEAR_BREAKEVEN", "MIXED") and "not a forecast" in out["verdict"]["line"]
    assert sum(b["n"] for b in sim["r_histogram"]) == 2000 and sim["r_spread"]["p05"] <= sim["r_spread"]["p50"] <= sim["r_spread"]["p95"]
    times = [r["time"] for r in out["by_entry_time"]]
    assert "10:30" in times and times == sorted(times) and sum(1 for r in out["by_entry_time"] if r["is_selected"]) == 1
    assert out["by_entry_time"][times.index("10:30")]["sim_p_target"] == sim["p_target"]
    assert out["history_vs_simulation_pts"] is not None and len(out["limits"]) == len(E.LIMITS) and out["elapsed_ms"] >= 0


def test_run_is_deterministic_for_a_seed_and_a_short_run_mirrors():
    tm_data.clear_cache()
    body = {"symbol": "SBIN", "sims": 1500, "seed": 9}
    a = tm.run(body, fetch=good_fetch, charges_fn=fake_charges)
    b = tm.run(body, fetch=good_fetch, charges_fn=fake_charges)
    assert a["simulation"] == b["simulation"] and a["history"] == b["history"] and a["verdict"] == b["verdict"]
    s = tm.run({**body, "side": "SHORT"}, fetch=good_fetch, charges_fn=fake_charges)
    assert s["params"]["side"] == "SHORT" and s["params"]["flat_by"] == config.SHORT_SQUAREOFF_TIME_IST and s["control"]["side"] == "LONG"
    assert s["control"]["simulation"]["n"] == a["simulation"]["n"], "same sessions for both sides"


def test_run_errors_and_switch_off():
    tm_data.clear_cache()
    raises(lambda: tm.run({"symbol": "bad symbol!"}, fetch=good_fetch, charges_fn=fake_charges), tm.TimeMachineError, "BAD_SYMBOL", 400)
    e = raises(lambda: tm.run({"symbol": "TCS", "stop_pct": 0}, fetch=good_fetch, charges_fn=fake_charges), tm.TimeMachineError, "BAD_PARAM", 400)
    assert e.as_dict()["ok"] is False

    def boom(sym, lb):
        raise tm_data.DataError("DATA_UNREACHABLE", "down", 502)
    raises(lambda: tm.run({"symbol": "WIPRO"}, fetch=boom, charges_fn=fake_charges), tm.TimeMachineError, "DATA_UNREACHABLE", 502)
    old = config.TIME_MACHINE_ENABLED
    config.TIME_MACHINE_ENABLED = False
    try:
        raises(lambda: tm.run({"symbol": "TCS"}, fetch=good_fetch, charges_fn=fake_charges), tm.TimeMachineError, "DISABLED", 503)
        raises(lambda: tm.compare({"symbols": ["TCS"]}, fetch=good_fetch, charges_fn=fake_charges), tm.TimeMachineError, "DISABLED", 503)
        assert tm.status()["enabled"] is False
    finally:
        config.TIME_MACHINE_ENABLED = old


def test_run_busy_when_two_runs_are_going():
    tm_data.clear_cache()
    assert tm._SEM.acquire(timeout=1) and tm._SEM.acquire(timeout=1)
    try:
        raises(lambda: tm.run({"symbol": "TCS", "sims": 1000}, fetch=good_fetch, charges_fn=fake_charges), tm.TimeMachineError, "BUSY", 429)
    finally:
        tm._SEM.release()
        tm._SEM.release()
    assert tm.run({"symbol": "TCS", "sims": 1000}, fetch=good_fetch, charges_fn=fake_charges)["ok"], "the slots are free again"


def test_compare_ranks_and_lists_failures():
    tm_data.clear_cache()

    def fetch(sym, lb):
        if sym == "NODATA":
            raise tm_data.DataError("NO_BARS", "none", 404)
        return many_days(15, {"AAA": 0.001, "BBB": 0.002, "CCC": 0.004}[sym])
    out = tm.compare({"symbols": ["aaa", "bbb", "ccc", "nodata", "AAA"], "sims": 40000}, fetch=fetch, charges_fn=fake_charges)
    assert out["ok"] and [r["symbol"] for r in out["failed"]] == ["NODATA"] and out["failed"][0]["code"] == "NO_BARS"
    assert sorted(r["symbol"] for r in out["rows"]) == ["AAA", "BBB", "CCC"], "a duplicate is run once"
    rs = [r["sim_avg_r_net"] for r in out["rows"]]
    assert rs == sorted(rs, reverse=True) and out["params"]["sims"] == tm.COMPARE_SIMS_CAP
    for bad in ({}, {"symbols": []}, {"symbols": "TCS"}, {"symbols": ["a b"]}, {"symbols": [f"S{i}" for i in range(16)]}, {"symbols": ["TCS"], "stop_pct": 0}):
        raises(lambda b=bad: tm.compare(b, fetch=fetch, charges_fn=fake_charges), tm.TimeMachineError)


def test_status_lists_switches_defaults_and_recent_runs():
    tm_data.clear_cache()
    tm.run({"symbol": "HDFCBANK", "sims": 1000}, fetch=good_fetch, charges_fn=fake_charges)
    s = tm.status()
    assert s["enabled"] is True and s["sims_default"] == config.TIME_MACHINE_SIMS and s["defaults"]["entry_time"] == "10:00" and s["max_compare_symbols"] == 15
    assert s["recent"][0]["symbol"] == "HDFCBANK" and s["cache"]["entries"] >= 1 and len(s["limits"]) == len(E.LIMITS)
    assert len(tm._recent) <= 20


def test_default_charges_fn_matches_the_ledger_when_it_can_be_imported():
    try:
        from orders import charges_ledger
    except Exception:  # noqa: BLE001 - sqlalchemy is not installed in every environment (the VM has it); the match is then checked there
        return
    fn = tm.default_charges_fn()
    for short in (False, True):
        assert abs(fn(100.0, 102.0, 200, short) - charges_ledger.charges_for_trade(100.0, 102.0, 200, short=short)["total"]) < 1e-9


# ------------------------------------------------------------------ wiring and guards (read as text)
def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_engine_is_stdlib_only_and_nothing_can_place_an_order():
    stdlib = set(sys.stdlib_module_names)
    tree = ast.parse(_src("timemachine/engine.py"))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= stdlib, mods - stdlib
    for rel in ("timemachine/engine.py", "timemachine/data.py", "timemachine/service.py"):
        text = _src(rel)
        for forbidden in ("dhan_client", "place_order", "place_super_order", "db.add", "SessionLocal", "sqlalchemy", "Session("):
            assert forbidden not in text, (rel, forbidden)


def test_three_routes_and_four_switches_exist():
    main = _src("main.py")
    for route in ('@app.get("/time-machine/status")', '@app.post("/time-machine/run")', '@app.post("/time-machine/compare")'):
        assert route in main, route
    cat = _src("control/catalog.py")
    runner = (ROOT.parent / "calibration-runner-service" / "autocalib.py")
    runner_text = runner.read_text(encoding="utf-8") if runner.exists() else None
    for k in ("TIME_MACHINE_ENABLED", "TIME_MACHINE_SIMS", "TIME_MACHINE_SLIPPAGE_PCT", "TIME_MACHINE_LOOKBACK"):
        assert f'Flag("{k}"' in cat and hasattr(config, k), k
        if runner_text is not None:
            assert re.search(rf'"{k}":', runner_text), f"{k} must be on the calibrator's PROTECTED list"


def test_config_defaults_are_inside_their_ranges():
    assert config.TIME_MACHINE_ENABLED is True and 1000 <= config.TIME_MACHINE_SIMS <= 50000 and 0 <= config.TIME_MACHINE_SLIPPAGE_PCT <= 1
    assert config.TIME_MACHINE_LOOKBACK in ("1mo", "2mo") and config.TIME_MACHINE_DEFAULT_VALUE_RS >= 1000


def test_two_runs_at_once_both_finish():
    tm_data.clear_cache()
    res, errs = [], []

    def go(sym):
        try:
            res.append(tm.run({"symbol": sym, "sims": 1000}, fetch=good_fetch, charges_fn=fake_charges)["symbol"])
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go, args=(s,)) for s in ("TCS", "INFY")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and sorted(res) == ["INFY", "TCS"]
