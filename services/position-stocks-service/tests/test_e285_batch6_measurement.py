"""E285 batch 6: measurement. Quoted bar-file names, no-bar causes, shared verdict rule, random-entry baseline, the scorecard report. No DB needed."""
import calendar
import json
import os
import random
import tempfile

import config
from feed import bar_recorder as br
from orders import candidate_outcomes as co, gate_policy as gp, gate_policy_job as job, scorecard as sc

G, B, N, E = gp.GAP_DOWN, gp.BELOW_OPEN, gp.NEAR_DAY_HIGH, gp.EVENT
DAY = "2026-10-27"


def _ts(day=DAY, hh=10, mm=0):
    y, m, d = (int(x) for x in day.split("-"))
    return calendar.timegm((y, m, d, hh, mm, 0)) - 19800


def _bars(day, gain, start_min=0, end_min=375):
    """1-minute bars: flat 100 until 10:00 IST, linear to 100+gain at 11:00, flat after."""
    y, m, d = (int(x) for x in day.split("-"))
    t_open = calendar.timegm((y, m, d, 3, 45, 0))
    out = []
    for k in range(start_min, end_min + 1):
        t = t_open + 60 * k
        frac = min(1.0, max(0.0, (t - (t_open + 45 * 60)) / 3600.0))
        px = 100.0 * (1 + gain / 100.0 * frac)
        out.append([t, px, px + 0.01, px - 0.01, px, 1000])
    return out


# ------------------------------------------------------------------ the file name bug

def test_readers_use_the_recorders_quoted_file_name():
    assert co.bar_path("/d", DAY, "M&M") == br.file_for("/d", DAY, "M&M") and co.bar_path("/d", DAY, "M&M").endswith("M%26M.json")
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, DAY))
        for sym in ("M&M", "TCS"):
            with open(co.bar_path(root, DAY, sym), "w", encoding="utf-8") as f:
                json.dump({"bars": _bars(DAY, 1.0)}, f)
        ld = co.file_loader(root)
        assert ld("M&M", DAY) and ld("TCS", DAY) and ld("NOPE", DAY) is None
        assert co.file_symbols(root)(DAY) == ["M&M", "TCS"] and co.file_symbols(root)("2026-01-01") is None


# ------------------------------------------------------------------ why a sample cannot be measured

def test_no_bar_reason_names_the_cause():
    t = _ts()
    assert co.no_bar_reason(None, t) == "no_bars" and co.no_bar_reason([], t) == "no_bars"
    assert co.no_bar_reason(_bars(DAY, 1.0, start_min=90), t) == "starts_after"        # first bar 10:45, the skip was at 10:00
    assert co.no_bar_reason(_bars(DAY, 1.0, end_min=100), t) == "ends_before"           # data stops at 10:55, +60 min is 11:00
    assert co.no_bar_reason(_bars(DAY, 1.0), t) == "ok"
    assert co.no_bar_reason(_bars(DAY, 1.0), _ts(hh=15, mm=0)) == "ends_before"         # a late skip: not a recording fault


# ------------------------------------------------------------------ one rule for the policy and the scorecard

GOOD = {"n": 40, "days": 6, "symbols": 25, "mean": 0.8, "lb": 0.3, "loo_min": 0.5}


def test_the_scorecard_verdict_and_the_policy_job_agree():
    cases = [GOOD, {**GOOD, "n": 29}, {**GOOD, "days": 4}, {**GOOD, "symbols": 19}, {**GOOD, "lb": 0.0}, {**GOOD, "loo_min": -0.01}, {**GOOD, "loo_min": None},
             {**GOOD, "mean": -0.4, "lb": -0.7, "loo_min": -0.3}, {**GOOD, "mean": 0.02, "lb": -0.2, "loo_min": -0.1}]
    for st in cases:
        moved = job.evaluate_gate(G, "HARD", st, "2026-11-02", None)["changed"]
        v = job.verdict(st)
        assert v.startswith("LOOSEN") == moved, (st, v)
        why = job.loosen_check(st)
        assert (not why) == moved and (not why or why in job.evaluate_gate(G, "HARD", st, "2026-11-02", None)["note"])


def test_verdict_words():
    assert job.verdict({"n": 5}) == "too few samples" and job.verdict({"n": 99}, is_baseline=True) == "baseline"
    assert job.verdict(GOOD).startswith("LOOSEN")
    assert job.verdict({**GOOD, "mean": -0.4, "lb": -0.7, "loo_min": -0.3}) == "KEEP: saves money"
    assert job.verdict({**GOOD, "mean": -0.10, "lb": -0.7, "loo_min": -0.3}).startswith("NO CALL")        # on the band edge: not under it
    assert job.verdict({**GOOD, "lb": 0.0}).startswith("NO CALL: bootstrap lower bound")


# ------------------------------------------------------------------ random-entry baseline

def test_baseline_is_deterministic_same_time_other_symbols_and_side_aware():
    syms = ["AAA", "BBB", "CCC", "DDD"]
    up = lambda s, d: _bars(d, 1.0)
    a = co.baseline_values(DAY, _ts(), "AAA", "LONG", syms, up, 3, 0.15, random.Random(1))
    assert len(a) == 3 and all(abs(v - 0.85) < 1e-6 for v in a)
    assert a == co.baseline_values(DAY, _ts(), "AAA", "LONG", syms, up, 3, 0.15, random.Random(1))
    seen = []
    co.baseline_values(DAY, _ts(), "AAA", "LONG", syms, lambda s, d: seen.append(s) or _bars(d, 1.0), 3, 0.15, random.Random(2))
    assert "AAA" not in seen                                                                     # never the symbol itself
    sh = co.baseline_values(DAY, _ts(), "AAA", "SHORT", syms, up, 2, 0.15, random.Random(1))
    assert all(abs(v - (-1.15)) < 1e-6 for v in sh)                                              # a rising market loses for a short
    assert co.baseline_values(DAY, _ts(), "AAA", "LONG", ["AAA"], up, 3, 0.15, random.Random(1)) == [] and co.baseline_values(DAY, _ts(), "AAA", "LONG", syms, up, 0, 0.15, random.Random(1)) == []


# ------------------------------------------------------------------ the report

def _rows():
    r = []
    for i in range(40):                                   # 40 gap-down skips on 6 days, 25 symbols, all rising 1 %
        day = f"2026-10-{20 + i % 6:02d}"
        r.append({"symbol": f"S{i % 25:02d}", "decision": "SKIPPED", "reason": "OPENING_GATE:GAP_DOWN:-1.50% < -1.00%", "created_ts": _ts(day)})
    r.append({"symbol": "S00", "decision": "SKIPPED", "reason": "OPENING_GATE:GAP_DOWN:-1.60% < -1.00%", "created_ts": _ts("2026-10-20", 10, 10)})        # same 30-min bucket: deduped
    r.append({"symbol": "S01", "decision": "SKIPPED", "reason": "SHORT_VWAP_ABOVE:x", "created_ts": _ts("2026-10-20")})                                   # a short row
    r.append({"symbol": "NOFILE", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH", "created_ts": _ts("2026-10-20")})                          # day folder, no file
    r.append({"symbol": "NODAY", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH", "created_ts": _ts("2026-10-12")})                           # no folder that day
    r.append({"symbol": "LATE", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH", "created_ts": _ts("2026-10-20")})                            # bars start after
    return r


def _env():
    days = {f"2026-10-{20 + i:02d}" for i in range(6)}
    names = [f"S{i:02d}" for i in range(25)] + ["LATE"]

    def load(sym, day):
        if sym == "LATE":
            return _bars(day, 1.0, start_min=90) if day in days else None
        if sym.startswith("S") and day in days:
            return _bars(day, 1.0)
        return None
    return load, (lambda day: names if day in days else None)


def test_report_coverage_causes_sides_baseline_and_verdict():
    load, lst = _env()
    res = sc.build(_rows(), load, lst, cost=0.15, direction="auto", baseline_k=3)
    c = res["coverage"]
    assert c["deduped"] == 1 and c["no_file"] == 1 and c["no_day_folder"] == {"2026-10-12": 1} and c["starts_after"] == 1 and c["measured"] == 41 and c["unique"] == 44
    by = {(x["gate"], x["side"]): x for x in res["groups"]}
    g = by[(G, "LONG")]
    assert g["stats"]["n"] == 40 and g["stats"]["days"] == 6 and g["stats"]["symbols"] == 25 and abs(g["stats"]["mean"] - 0.85) < 1e-6
    assert abs(g["rand60"] - 0.85) < 1e-6 and abs(g["edge"]) < 1e-6 and g["verdict"].startswith("LOOSEN")        # passes the rule, but no better than random: the report says so
    assert any("edge over random entries" in n for n in res["notes"])
    assert ("SHORT_VWAP_ABOVE", "SHORT") in by and abs(by[("SHORT_VWAP_ABOVE", "SHORT")]["stats"]["mean"] - (-1.15)) < 1e-6          # measured as a short: a rising stock lost
    text = sc.render(res, 15, 30, by_day=True)
    assert "unique samples" in text and "no bar folder for the day 1" in text and "bars start after the skip 1" in text and "by day:" in text and "LOOSEN" in text


def test_long_and_short_on_one_gate_are_not_averaged_and_a_long_run_warns():
    load, lst = _env()
    t = _ts("2026-10-20")
    rows = [{"symbol": "S00", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH", "created_ts": t},
            {"symbol": "S01", "decision": "SKIPPED", "reason": "SHORT_X", "created_ts": t}]
    res = sc.build(rows, load, lst, direction="auto", baseline_k=0)
    assert {(x["gate"], x["side"]) for x in res["groups"]} == {("RANGE_GATE:NEAR_DAY_HIGH", "LONG"), ("SHORT_X", "SHORT")}
    res2 = sc.build(rows, load, lst, direction="long", baseline_k=0)
    assert any("measured as longs" in n and "INVERTED" in n for n in res2["notes"])
    res3 = sc.build(rows[:1], load, lst, direction="short", baseline_k=0)
    assert res3["groups"][0]["side"] == "SHORT"


def test_low_coverage_is_flagged():
    load, lst = _env()
    rows = [{"symbol": f"MISSING{i}", "decision": "SKIPPED", "reason": "X_GATE", "created_ts": _ts("2026-10-20")} for i in range(5)] + _rows()[:2]
    res = sc.build(rows, load, lst, baseline_k=0)
    assert res["coverage"]["no_file"] == 5 and any("could be measured" in n for n in res["notes"])


# ------------------------------------------------------------------ the recorder names what it never saw

def test_recorder_lists_the_noted_symbols_the_store_never_saw():
    NOW = 1_793_000_000.0
    DN = int((NOW + br.IST_OFFSET_S) // 86400)
    with tempfile.TemporaryDirectory() as root:
        br.reset()
        config.BAR_RECORD_DIR, config.BAR_RECORD_MAX_SYMBOLS, config.BAR_RECORD_ENABLED = root, 5, True
        br.note_symbols(["ZZZ", "YYY", "AAA"], NOW)
        bars = {s: [[NOW - 60 * (30 - i), 10.0, 11.0, 9.0, 10.0, 5] for i in range(30)] for s in ("AAA", "BBB")}
        br.run_once(NOW, True, bars_fn=lambda s: bars[s], symbols_fn=lambda: ["AAA", "BBB"], day_fn=lambda: DN)
        s = br.stats()
        assert s["priority_missing"] == 2 and s["priority_missing_names"] == ["YYY", "ZZZ"]
        br.reset()
        assert br.stats()["priority_missing_names"] == []


def test_the_script_is_a_thin_wrapper():
    s = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "gate_scorecard.py"), encoding="utf-8").read()
    assert "sc.build(" in s and "co.file_loader(" in s and "--by-day" in s and "--baseline-k" in s and "def forward" not in s


def test_recorder_says_why_a_noted_symbol_has_no_bars():
    NOW = 1_793_000_000.0
    DN = int((NOW + br.IST_OFFSET_S) // 86400)
    with tempfile.TemporaryDirectory() as root:
        br.reset()
        config.BAR_RECORD_DIR, config.BAR_RECORD_MAX_SYMBOLS, config.BAR_RECORD_ENABLED = root, 5, True
        br.note_symbols(["ZZZ", "YYY", "AAA"], NOW)
        bars = {"AAA": [[NOW - 60 * (30 - i), 10.0, 11.0, 9.0, 10.0, 5] for i in range(30)]}
        br.run_once(NOW, True, bars_fn=lambda s: bars[s], symbols_fn=lambda: ["AAA"], day_fn=lambda: DN, subscribed_fn=lambda: {"AAA", "YYY"})
        s = br.stats()
        assert s["priority_missing_cause_known"] and s["priority_missing_not_subscribed"] == ["ZZZ"] and s["priority_missing_no_tick"] == ["YYY"]
        br.reset()
        br.note_symbols(["ZZZ"], NOW)
        br.run_once(NOW, True, bars_fn=lambda s: bars["AAA"], symbols_fn=lambda: ["AAA"], day_fn=lambda: DN, subscribed_fn=lambda: set())
        s = br.stats()                                                             # no subscription list known: no claim either way
        assert not s["priority_missing_cause_known"] and s["priority_missing_not_subscribed"] == [] and s["priority_missing_no_tick"] == []
        br.reset()

