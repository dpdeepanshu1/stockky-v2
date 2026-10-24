"""Part 1 step 3: the runner's regime/ package (data, features, classifier, profiles) and scripts/regime_history_replay.py.
Offline, stdlib only (no pytest fixtures). Run: python -m pytest tests/test_regime_package.py -q   or   python tests/test_regime_package.py"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

from regime import STATES, classifier, data, features, profiles  # noqa: E402
import regime_history_replay as rr  # noqa: E402


def _bars(n, start=18000.0, step=0.0, rng=0.8, d0="2022-01-03"):
    """n synthetic daily bars: close moves by `step` %, every day spans `rng` %."""
    out, c, d = [], start, datetime.fromisoformat(d0)
    for _ in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o, cl = c, c * (1 + step / 100)
        out.append({"d": d.date().isoformat(), "o": o, "h": max(o, cl) * (1 + rng / 200), "l": min(o, cl) * (1 - rng / 200), "c": cl, "v": 1.0})
        c, d = cl, d + timedelta(days=1)
    return out


# ------------------------------------------------------------------------------------------------------ data
def test_clean_daily_drops_bad_rows_dedupes_and_sorts():
    doc = {"candles": [
        {"date": "2024-01-03", "open": 10, "high": 12, "low": 9, "close": 11},
        {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5},
        {"date": "2024-01-03", "open": 10, "high": 13, "low": 9, "close": 12},      # same day again: the later row wins
        {"date": "2024-01-04", "open": 0, "high": 5, "low": 4, "close": 4},          # zero price
        {"date": "2024-01-05", "open": 10, "high": 8, "low": 9, "close": 9},         # high < low
        {"date": "2024-01-06", "open": 10, "high": 11, "low": 9, "close": 15},       # close above high
        {"date": "garbage", "open": 1, "high": 1, "low": 1, "close": 1},
        {"date": "2024-01-08", "open": "x", "high": 1, "low": 1, "close": 1},
        "not a dict"]}
    got = data.clean_daily(doc)
    assert [b["d"] for b in got] == ["2024-01-02", "2024-01-03"]
    assert got[1]["c"] == 12


def test_epoch_dates_and_bare_lists_work():
    ms = int(datetime(2024, 3, 4, tzinfo=timezone.utc).timestamp() * 1000)
    got = data.clean_daily([{"timestamp": ms, "open": 1, "high": 2, "low": 1, "close": 2}])
    assert got and got[0]["d"] == "2024-03-04"


def test_upto_is_strictly_before_the_day():
    bs = _bars(10)
    assert data.upto(bs, bs[5]["d"])[-1]["d"] == bs[4]["d"]
    assert data.upto(bs, bs[5]["d"], include_day=True)[-1]["d"] == bs[5]["d"]


# ------------------------------------------------------------------------------------------------------ features
def test_atr_and_missing_data_are_honest():
    bs = _bars(30, rng=1.0)
    a = features.atr_pct(bs, 14)
    assert a is not None and 0.9 < a < 1.2
    assert features.atr_pct(_bars(5), 14) is None
    f = features.daily_features(_bars(3))
    assert f["atr14_pct"] is None and f["ret_20d"] is None and f["vix"] is None


def test_vix_features_and_percentile():
    vix = [{"d": f"d{i}", "o": 1, "h": 1, "l": 1, "c": 12.0 + (i % 5)} for i in range(120)]
    vix[-1]["c"] = 30.0
    f = features.daily_features(_bars(60), vix)
    assert f["vix"] == 30.0 and f["vix_pctile"] == 100.0 and f["vix_chg_1d_pct"] >= 100


def test_opening_and_intraday_features():
    o = features.opening_features(100.0, 97.0, 1.0)
    assert round(o["gap_pct"], 2) == -3.0 and round(o["gap_x_atr"], 2) == 3.0
    assert features.opening_features(None, 97.0, 1.0)["gap_pct"] is None
    bars = [{"t": "09:15", "o": 100, "h": 101, "l": 99, "c": 100}, {"t": "09:20", "o": 100, "h": 100, "l": 97, "c": 98}]
    i = features.intraday_features(100.0, bars, 1.0)
    assert round(i["day_ret_pct"], 2) == -2.0 and round(i["fall_from_high_pct"], 2) == round((1 - 98 / 101) * 100, 2) and i["range_x_atr"] == 4.0
    assert features.intraday_features(100.0, [], 1.0)["day_ret_pct"] is None


# ------------------------------------------------------------------------------------------------------ classifier
CALM = {"vix": 12.5, "vix_pctile": 20, "vix_chg_1d_pct": -3, "atr14_pct": 0.7, "rv10_pct": 0.5, "ret_5d": 1.0, "drawdown_20d_pct": 0.5,
        "below_sma20_pct": 0, "last_range_x_atr": 0.9, "down_days_5": 2}
STRESS = {"vix": 27, "vix_pctile": 97, "vix_chg_1d_pct": 12, "atr14_pct": 1.7, "rv10_pct": 1.4, "ret_5d": -3.5, "drawdown_20d_pct": 6,
          "below_sma20_pct": 3.5, "last_range_x_atr": 2.2, "down_days_5": 4}


def test_calm_and_stressed_inputs_and_order():
    assert classifier.classify(CALM)["state"] == "calm"
    s = classifier.classify(STRESS)
    assert s["state"] in ("stressed", "panic") and s["data_ok"] and s["reasons"]
    mid = {k: (CALM[k] + STRESS[k]) / 2 for k in CALM}
    assert classifier.ordinal(classifier.classify(CALM)["state"]) < classifier.ordinal(classifier.classify(mid)["state"]) <= classifier.ordinal(s["state"])


def test_score_is_monotone_when_one_feature_worsens():
    last = -1.0
    for v in (12, 15, 18, 22, 26, 30):
        sc = classifier.classify(dict(CALM, vix=v))["score"]
        assert sc >= last
        last = sc


def test_missing_data_never_tightens():
    r = classifier.classify({})
    assert r["state"] == "normal" and r["data_ok"] is False and r["score"] is None
    r = classifier.classify({"vix": 20})                          # one input only: below the coverage floor
    assert r["state"] == "normal" and r["data_ok"] is False


def test_floors_apply_even_without_enough_data():
    assert classifier.classify({"vix": 31})["state"] == "stressed"
    assert classifier.classify({"vix": 40})["state"] == "panic"
    assert classifier.classify({"gap_pct": -3.2})["state"] == "panic"
    assert classifier.classify(dict(CALM, gap_pct=-2.5))["state"] == "stressed"
    assert classifier.classify({"day_ret_pct": -2.1})["state"] == "stressed"
    assert "VIX at or above 30" in classifier.classify({"vix": 31})["floor_reasons"]


def test_ratchet_only_goes_up():
    r = classifier.classify(CALM, prev_state="stressed", ratchet=True)
    assert r["state"] == "stressed" and r["held"] is True
    assert classifier.classify(CALM, prev_state="stressed", ratchet=False)["state"] == "calm"
    assert classifier.classify(STRESS, prev_state="calm", ratchet=True)["state"] != "calm"


def test_classifier_reads_only_features_given():
    a = classifier.classify(STRESS)
    b = classifier.classify(copy.deepcopy(STRESS))
    assert a == b


# ------------------------------------------------------------------------------------------------------ profiles
NOW_IST_MORNING = datetime(2026, 10, 23, 3, 30, tzinfo=timezone.utc)           # 09:00 IST
SAVED_RT = {"MARKET_STATE_RISK_MULT": "1.0", "MARKET_STATE_CONVICTION_ADD": "0", "OPENING_ENTRY_NOT_BEFORE_IST": "09:15",
            "RISK_MAX_ENTRIES_PER_DAY": "6", "ENTRY_LAST_TIME_IST": "14:00"}
SAVED_PS = {"MARKET_STATE_RISK_MULT": "1.0", "MAX_CONCURRENT_SCALP_POSITIONS": "5", "SCALP_ENTRY_CUTOFF_IST": "14:00"}


def test_calm_and_normal_are_stamp_only():
    for st in ("calm", "normal"):
        o = profiles.build_overlay("real-trade", st, SAVED_RT, NOW_IST_MORNING)
        assert o["entries"] == {} and o["state"] == st and o["version"] == profiles.VERSION and o["until"].endswith("15:30:00+05:30")


def test_every_entry_tightens_the_saved_value_and_values_are_canonical():
    for svc, saved in (("real-trade", SAVED_RT), ("position-stocks", SAVED_PS)):
        for st in STATES:
            o = profiles.build_overlay(svc, st, saved, NOW_IST_MORNING)
            for k, v in o["entries"].items():
                assert profiles.compare(k, saved[k], v) == "TIGHTENS", (svc, st, k)
                assert svc in profiles.DIALS[k]["services"]
    o = profiles.build_overlay("real-trade", "stressed", SAVED_RT, NOW_IST_MORNING)
    assert o["entries"] == {"MARKET_STATE_RISK_MULT": "0.6", "MARKET_STATE_CONVICTION_ADD": "6", "OPENING_ENTRY_NOT_BEFORE_IST": "09:45",
                            "RISK_MAX_ENTRIES_PER_DAY": "3", "ENTRY_LAST_TIME_IST": "13:00"}


def test_a_dial_the_person_already_set_tighter_is_never_loosened():
    saved = dict(SAVED_RT, RISK_MAX_ENTRIES_PER_DAY="2", ENTRY_LAST_TIME_IST="12:30", MARKET_STATE_RISK_MULT="0.5")
    o = profiles.build_overlay("real-trade", "choppy", saved, NOW_IST_MORNING)
    assert "RISK_MAX_ENTRIES_PER_DAY" not in o["entries"] and "ENTRY_LAST_TIME_IST" not in o["entries"] and "MARKET_STATE_RISK_MULT" not in o["entries"]
    assert {s["key"] for s in o["skipped"]} >= {"RISK_MAX_ENTRIES_PER_DAY", "ENTRY_LAST_TIME_IST", "MARKET_STATE_RISK_MULT"}


def test_unknown_or_unreadable_saved_values_are_skipped_not_guessed():
    o = profiles.build_overlay("real-trade", "panic", {"MARKET_STATE_RISK_MULT": "abc", "ENTRY_LAST_TIME_IST": "25:99"}, NOW_IST_MORNING)
    assert o["entries"] == {} and len(o["skipped"]) == 5


def test_profile_gets_stricter_from_choppy_to_panic():
    for k, d in profiles.DIALS.items():
        if k in profiles.STATE_SPECIFIC_DIALS:
            continue                      # the short dial follows the plan's short table (off in choppy, back in stressed / panic), see test_short_dial_*
        vals = [profiles.PROFILE[s][k] for s in STATES[2:] if k in profiles.PROFILE[s]]
        for a, b in zip(vals, vals[1:]):
            assert profiles.compare(k, a, b) in ("TIGHTENS", "SAME"), (k, a, b)


def test_short_dial_follows_the_plan_table_and_only_reaches_position_stocks():
    key = "MARKET_STATE_SHORT_MULT"
    want = {"calm": 0.0, "normal": None, "choppy": 0.0, "stressed": None, "panic": 0.5}
    for st, v in want.items():
        assert profiles.PROFILE[st].get(key) == v, st
    saved = dict(SAVED_PS, **{key: "1.0"})
    assert profiles.build_overlay("position-stocks", "calm", saved, NOW_IST_MORNING)["entries"] == {key: "0"}
    assert profiles.build_overlay("position-stocks", "choppy", saved, NOW_IST_MORNING)["entries"][key] == "0"
    assert profiles.build_overlay("position-stocks", "panic", saved, NOW_IST_MORNING)["entries"][key] == "0.5"
    for st in ("normal", "stressed"):
        assert key not in profiles.build_overlay("position-stocks", st, saved, NOW_IST_MORNING)["entries"]
    for st in STATES:                                                                 # real-trade is long-only: it never receives the short dial
        assert key not in profiles.build_overlay("real-trade", st, dict(SAVED_RT, **{key: "1.0"}), NOW_IST_MORNING)["entries"]


def test_short_dial_is_never_loosened():
    saved = dict(SAVED_PS, MARKET_STATE_SHORT_MULT="0.25")                            # a person already set it tighter than panic's 0.5
    o = profiles.build_overlay("position-stocks", "panic", saved, NOW_IST_MORNING)
    assert "MARKET_STATE_SHORT_MULT" not in o["entries"]
    o = profiles.build_overlay("position-stocks", "calm", dict(SAVED_PS, MARKET_STATE_SHORT_MULT="0"), NOW_IST_MORNING)
    assert "MARKET_STATE_SHORT_MULT" not in o["entries"]


def test_expiry_is_1530_ist_and_none_after_it():
    assert profiles.expiry(NOW_IST_MORNING).isoformat() == "2026-10-23T15:30:00+05:30"
    late = datetime(2026, 10, 23, 10, 5, tzinfo=timezone.utc)                    # 15:35 IST
    assert profiles.expiry(late) is None
    assert profiles.build_overlay("real-trade", "panic", SAVED_RT, late)["until"] is None
    ist_day = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)                  # 01:30 IST on the 24th: that day's 15:30
    assert profiles.expiry(ist_day).isoformat() == "2026-10-24T15:30:00+05:30"


def test_bad_arguments_raise():
    for args in (("nope", "calm"), ("real-trade", "weird")):
        try:
            profiles.build_overlay(args[0], args[1], {}, NOW_IST_MORNING)
        except ValueError:
            continue
        raise AssertionError(args)


def test_dockerfile_copies_the_regime_package_and_the_job_is_registered():
    text = (HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY services/calibration-runner-service/regime /app/regime" in text
    for f in (HERE / "regime").glob("*.py"):
        assert f.stat().st_size > 0
    import jobs
    j = jobs.JOBS["regime_history_replay"]
    steps = j["builder"]({"python": sys.executable, "scripts": str(REPO / "scripts"), "md": "http://md", "run": "/tmp/run"}, {"period": "2y"})
    assert steps[0]["argv"][1].endswith("regime_history_replay.py") and "--base" in steps[0]["argv"] and "2y" in steps[0]["argv"]
    assert (REPO / "scripts" / "regime_history_replay.py").exists()


def test_version_fits_the_kit_rule():
    assert re.match(r"^[A-Za-z0-9._-]{1,24}$", profiles.VERSION)


_PROBE = r"""
import json, sys
sys.path.insert(0, '.')
from control import catalog
print(json.dumps({f.key: {"kind": f.kind, "looser": f.looser, "overlay": f.overlay, "live": f.live, "shipped": f.shipped} for f in catalog.CATALOG}))
"""


def _catalog(service_dir: str):
    p = REPO / "services" / service_dir
    if not (p / "control" / "catalog.py").exists():
        return None
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(p), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_dials_match_both_trading_catalogs():
    """Every dial a profile names must be an overlay switch with the same direction in each service that it is listed for - and vice versa."""
    cats = {"real-trade": _catalog("real-trade-service"), "position-stocks": _catalog("position-stocks-service")}
    if not any(cats.values()):
        return                                   # runner image without the other services: nothing to compare
    for svc, cat in cats.items():
        if cat is None:
            continue
        overlay_keys = {k for k, f in cat.items() if f["overlay"]}
        mine = {k for k, d in profiles.DIALS.items() if svc in d["services"]}
        assert mine == overlay_keys, (svc, sorted(mine ^ overlay_keys))
        for k in mine:
            assert cat[k]["looser"] == profiles.DIALS[k]["looser"], (svc, k)
            assert cat[k]["kind"] == profiles.DIALS[k]["kind"], (svc, k)
            assert cat[k]["live"] is True
    for svc, saved in (("real-trade", SAVED_RT), ("position-stocks", SAVED_PS)):       # the test inputs above are the real shipped values
        if cats[svc]:
            for k, v in saved.items():
                assert str(cats[svc][k]["shipped"]) in (v, v.rstrip("0").rstrip(".")), (svc, k, v, cats[svc][k]["shipped"])


def test_kit_accepts_what_the_profiles_produce():
    """Run the real kit.classify / canonical on every entry: TIGHTENS against the shipped value, and the string is already canonical."""
    code = r"""
import json, sys
sys.path.insert(0, '.'); sys.path.insert(0, %r)
from control import catalog, kit
from datetime import datetime, timezone
from regime import profiles, STATES
by = {f.key: f for f in catalog.CATALOG}
saved = {k: f.shipped for k, f in by.items() if k in profiles.DIALS}
svc = "real-trade" if catalog.SERVICE.startswith("real") else "position-stocks"
bad = []
for st in STATES:
    o = profiles.build_overlay(svc, st, saved, datetime(2026, 10, 23, 3, 30, tzinfo=timezone.utc))
    for k, v in o["entries"].items():
        f = by[k]
        c = kit.canonical(f, v)
        if c != v or kit.classify(f, kit.canonical(f, saved[k]), c) != "TIGHTENS":
            bad.append([st, k, v, c])
print(json.dumps(bad))
""" % str(HERE)
    for d in ("real-trade-service", "position-stocks-service"):
        p = REPO / "services" / d
        if not (p / "control" / "kit.py").exists():
            continue
        r = subprocess.run([sys.executable, "-c", code], cwd=str(p), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-800:]
        assert json.loads(r.stdout.strip().splitlines()[-1]) == [], d


# ------------------------------------------------------------------------------------------------------ replay
def _clustered(n=700):
    """Index bars where volatile days cluster in blocks, with a VIX that is high inside those blocks (so a useful classifier exists)."""
    import random
    rnd = random.Random(11)
    nb, vb, c = [], [], 18000.0
    d = datetime(2021, 1, 4)
    calm, i = True, 0
    while i < n:
        block = rnd.randint(15, 40)
        for _ in range(block):
            if i >= n:
                break
            while d.weekday() >= 5:
                d += timedelta(days=1)
            sig = 0.45 if calm else 1.6
            r = rnd.gauss(0 if calm else -0.2, sig) / 100
            o, cl = c, c * (1 + r)
            h = max(o, cl) * (1 + abs(rnd.gauss(0, sig * 0.5)) / 100)
            l = min(o, cl) * (1 - abs(rnd.gauss(0, sig * 0.5)) / 100)
            nb.append({"date": d.date().isoformat(), "open": o, "high": h, "low": l, "close": cl, "volume": 1})
            v = (13 if calm else 27) + rnd.gauss(0, 1.2)
            vb.append({"date": d.date().isoformat(), "open": v, "high": v, "low": v, "close": v})
            c, d, i = cl, d + timedelta(days=1), i + 1
        calm = not calm if rnd.random() < 0.75 else calm
    return {"candles": nb}, {"candles": vb}


def test_replay_has_no_lookahead():
    nd, vd = _clustered(300)
    nifty, vix = data.clean_daily(nd), data.clean_daily(vd)
    base = rr.replay(nifty, vix)
    i = len(nifty) - 40
    mutated = copy.deepcopy(nifty)
    for b in mutated[i:]:                                                   # rewrite day i and everything after it
        b.update(o=b["o"] * 1.3, h=b["h"] * 1.3, l=b["l"] * 0.9, c=b["c"] * 0.7)
    mv = copy.deepcopy(vix)
    for b in mv:
        if b["d"] >= nifty[i]["d"]:
            b["c"] = 80.0
    again = rr.replay(mutated, mv)
    by_d = {r["d"]: r for r in again}
    first = base[0]["d"]
    same = [r for r in base if r["d"] <= nifty[i]["d"]]
    assert same and first in by_d
    for r in same:
        assert (by_d[r["d"]]["state"], by_d[r["d"]]["score"]) == (r["state"], r["score"]), r["d"]   # the morning state of day i sees only days before i


def test_spearman_and_ranks():
    assert rr.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert rr.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert rr.spearman([1, 1, 1], [1, 2, 3]) is None
    assert rr.ranks([5, 5, 1]) == [2.5, 2.5, 1.0]


def test_replay_finds_the_edge_in_clustered_data_and_not_in_noise():
    nd, vd = _clustered(1100)
    nifty, vix = data.clean_daily(nd), data.clean_daily(vd)
    rows = rr.replay(nifty, vix)
    v = rr.verdict(rows, rr.summarise(rows), True)
    assert v["verdict"] in ("USEFUL", "USEFUL_NOT_BEYOND_ATR"), v
    assert v["tight_vs_easy_range_ratio"] > 1.25 and v["states_in_order"] is True
    import random
    rnd = random.Random(5)                                                  # no clustering at all, VIX unrelated to the days
    flat = _bars(1100, rng=1.0)
    for b in flat:
        r = rnd.gauss(0, 0.9) / 100
        b["c"] = b["o"] * (1 + r)
        b["h"], b["l"] = max(b["o"], b["c"]) * 1.004, min(b["o"], b["c"]) * 0.996
    fv = [{"d": b["d"], "o": 1, "h": 1, "l": 1, "c": 12 + rnd.random() * 20} for b in flat]
    rows = rr.replay(flat, fv)
    assert rr.verdict(rows, rr.summarise(rows), True)["verdict"] in ("NO_EDGE", "NOT_ENOUGH_DATA")


def test_replay_needs_enough_days():
    nifty = _bars(150)
    rows = rr.replay(nifty, None)
    assert rr.verdict(rows, rr.summarise(rows), False)["verdict"] == "NOT_ENOUGH_DATA"


def test_script_runs_from_files_and_writes_reports():
    nd, vd = _clustered(900)
    with tempfile.TemporaryDirectory() as t:
        n, v, out = Path(t, "n.json"), Path(t, "v.json"), Path(t, "out")
        n.write_text(json.dumps(nd))
        v.write_text(json.dumps(vd))
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "regime_history_replay.py"), "--nifty", str(n), "--vix", str(v), "--out", str(out)],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode in (0, 1), r.stderr[-500:]
        assert "VERDICT:" in r.stdout and (out / "regime_replay.txt").exists()
        doc = json.loads((out / "regime_replay.json").read_text())
        assert doc["summary"]["verdict"] in ("USEFUL", "USEFUL_NOT_BEYOND_ATR", "NO_EDGE", "NOT_ENOUGH_DATA") and set(doc["per_state"]) == set(STATES)
        assert subprocess.run([sys.executable, str(REPO / "scripts" / "regime_history_replay.py")], capture_output=True, text=True).returncode == 2


if __name__ == "__main__":
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-q"]))
    except ImportError:
        fails = 0
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print("ok  ", name)
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    print("FAIL", name, repr(e)[:400])
        sys.exit(1 if fails else 0)
