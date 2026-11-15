"""E579 round 554: scripts/drift_monitor.py (recent trades vs the strategy's own earlier history) and the registry's drift rule. Pure; synthetic records."""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drift_monitor as dm
import strategy_registry as sr
from test_evidence_report import rec


def closed(pid, net, tab="swing", label="BUY NOW", n=0):
    d = (date(2026, 9, 1) + timedelta(days=n // 3)).isoformat()
    return rec(pid, net, tab=tab, label=label, opened_at=f"{d}T04:00:00+00:00", closed_at=f"{d}T09:{n % 60:02d}:00+00:00")


def run_of(pid0, means, sd=8.0, seed=1, **kw):
    """records with a mean-net-rupee drift: means = [(count, mean)] in time order"""
    rnd = random.Random(seed)
    out, n = [], 0
    for cnt, m in means:
        for _ in range(cnt):
            out.append(closed(pid0 + n, rnd.gauss(m, sd), n=n, **kw))
            n += 1
    return out


def test_welch_z_and_classify_edges():
    assert dm.welch_z([1.0], [1.0, 2.0]) is None and dm.welch_z([1.0, 1.0], [2.0, 2.0]) is None        # not enough, or no spread
    assert dm.welch_z([0.0, 1.0, 2.0], [2.0, 3.0, 4.0]) < 0
    assert dm.classify(None, 1.0) == "STABLE"
    assert dm.classify(-3.0, -0.5) == "DECAY"
    assert dm.classify(-3.0, 0.5) == "WARNING", "a drop that is still positive is only a warning"
    assert dm.classify(-1.8, -1.0) == "WARNING" and dm.classify(-1.0, -1.0) == "STABLE" and dm.classify(2.0, 1.0) == "IMPROVING"


def test_a_clear_decay_is_flagged_and_a_steady_strategy_is_not():
    decay = dm.cell(run_of(1, [(60, 12.0), (20, -30.0)]), 20, 30)
    assert decay["state"] == "DECAY" and decay["z"] < -2.33 and decay["recent_mean_pct"] < 0 < decay["base_mean_pct"] and decay["recent_n"] == 20 and decay["base_n"] == 60
    steady = dm.cell(run_of(1, [(80, 6.0)], seed=3), 20, 30)
    assert steady["state"] in ("STABLE",) and steady["z"] is not None
    better = dm.cell(run_of(1, [(60, 0.0), (20, 25.0)], seed=2), 20, 30)
    assert better["state"] == "IMPROVING"


def test_thin_history_is_not_judged():
    assert dm.cell(run_of(1, [(49, 5.0)]), 20, 30)["state"] == "NOT_ENOUGH_DATA"      # 29 earlier + 20 recent: one short
    assert dm.cell(run_of(1, [(50, 5.0)]), 20, 30)["state"] != "NOT_ENOUGH_DATA"
    assert dm.cell([], 20, 30)["state"] == "NOT_ENOUGH_DATA"


def test_order_is_by_close_time_not_by_file_order_and_garbage_rows_are_skipped():
    rows = run_of(1, [(60, 12.0), (20, -30.0)])
    shuffled = list(reversed(rows))
    assert dm.cell(shuffled, 20, 30)["state"] == dm.cell(rows, 20, 30)["state"] == "DECAY"
    junk = rows + [{"position_id": 9999}, {"position_id": 9998, "net_pnl": "x", "entry_price": 0}]
    assert dm.cell(junk, 20, 30)["n"] == 80


def test_report_groups_by_strategy_and_names_the_alarm():
    recs = run_of(1, [(60, 12.0), (20, -30.0)], tab="volume_shock", label="WATCH") + run_of(1000, [(80, 6.0)], seed=3, tab="swing", label="BUY NOW")
    rep = dm.build_report(recs, 20, 30, {"mode": "REAL", "origin": "system"})
    assert rep["strategies"]["volume_shock | WATCH"]["state"] == "DECAY" and rep["strategies"]["swing | BUY NOW"]["state"] == "STABLE"
    assert "volume_shock | WATCH" in dm.alarms(rep) and "swing | BUY NOW" not in dm.alarms(rep)
    txt = dm.render(rep)
    assert "LOOK AT: volume_shock | WATCH" in txt and "smoke alarm" in txt


def test_main_exit_codes_and_json(tmp_path):
    p, out = str(tmp_path / "rt.json"), str(tmp_path / "o" / "d.json")
    json.dump({"records": run_of(1, [(60, 12.0), (20, -30.0)])}, open(p, "w"))
    assert dm.main([p, "--json", out]) == 2 and json.load(open(out))["strategies"]["swing | BUY NOW"]["state"] == "DECAY"
    json.dump({"records": run_of(1, [(80, 6.0)], seed=3)}, open(p, "w"))
    assert dm.main([p]) == 0
    json.dump({"records": []}, open(p, "w"))
    assert dm.main([p]) == 1
    assert dm.main([str(tmp_path / "missing.json")]) == 1


# ---------------------------------------------------------------------------------------------------- registry: 3 days of DECAY move a proven strategy to watch
KEY = "swing | BUY NOW"


def _proven():
    s = {"version": 1, "strategies": {KEY: {"status": "proven", "first_seen": "2026-11-01", "last_seen": None, "streak": {"verdict": "POSITIVE", "days": 0, "last_day": None}, "last": {}, "history": []}}, "updated": None}
    return s


def drift(i, state, key=KEY):
    return {"generated_at": f"{(date(2026, 11, 16) + timedelta(days=i)).isoformat()}T11:00:00+00:00", "strategies": {key: {"state": state, "z": -3.1}}}


def test_three_separate_decay_days_move_proven_to_watch_not_two():
    s = _proven()
    for i in range(2):
        assert sr.apply_drift(s, drift(i, "DECAY"), "2026-11-16") == []
    ch = sr.apply_drift(s, drift(2, "DECAY"), "2026-11-18")
    assert ch and ch[0]["to"] == "watch" and s["strategies"][KEY]["status"] == "watch" and s["strategies"][KEY]["history"][-1]["why"].startswith("3 days in a row of DECAY")


def test_same_day_rerun_and_other_states_never_advance_or_keep_the_count():
    s = _proven()
    for _ in range(5):
        sr.apply_drift(s, drift(0, "DECAY"), "2026-11-16")
    assert s["strategies"][KEY]["drift_streak"]["days"] == 1
    sr.apply_drift(s, drift(1, "DECAY"), "2026-11-17")
    sr.apply_drift(s, drift(2, "WARNING"), "2026-11-18")           # not DECAY: the count restarts
    assert s["strategies"][KEY]["drift_streak"]["days"] == 0 and s["strategies"][KEY]["status"] == "proven"
    sr.apply_drift(s, drift(3, "NOT_ENOUGH_DATA"), "2026-11-19")   # no information: nothing changes
    assert s["strategies"][KEY]["drift_streak"]["last_day"] == "2026-11-18"


def test_drift_never_promotes_and_only_touches_proven_and_known_strategies():
    s = _proven(); s["strategies"][KEY]["status"] = "testing"
    for i in range(6):
        sr.apply_drift(s, drift(i, "DECAY"), "2026-11-16")
    assert s["strategies"][KEY]["status"] == "testing"
    for i in range(6):
        sr.apply_drift(s, drift(i + 10, "IMPROVING"), "2026-11-16")
    assert s["strategies"][KEY]["status"] == "testing"
    assert sr.apply_drift(s, drift(0, "DECAY", key="unknown | x"), "2026-11-16") == [] and "unknown | x" not in s["strategies"]
    s["strategies"][KEY]["status"] = "retired"
    for i in range(20, 26):
        sr.apply_drift(s, drift(i, "DECAY"), "2026-11-16")
    assert s["strategies"][KEY]["status"] == "retired"
    assert sr.apply_drift(s, {}, "2026-11-16") == [] and sr.apply_drift(s, {"strategies": {KEY: "x"}}, "2026-11-16") == []


def test_main_uses_drift_only_when_fresh_and_readable(tmp_path):
    store, ev, dr = str(tmp_path / "reg.json"), str(tmp_path / "ev.json"), str(tmp_path / "dr.json")
    sr.save_store(store, _proven())
    for i in range(3):
        json.dump({"generated_at": f"{(date(2026, 11, 16) + timedelta(days=i)).isoformat()}T11:00:00+00:00", "groups": {"strategy": {KEY: {"n": 40, "verdict": "NOT_ENOUGH_DATA"}}}}, open(ev, "w"))
        json.dump(drift(i, "DECAY"), open(dr, "w"))
        assert sr.main(["--evidence", ev, "--drift", dr, "--store", store, "--today", (date(2026, 11, 16) + timedelta(days=i)).isoformat()]) == 0
    assert sr.load_store(store)["strategies"][KEY]["status"] == "watch"
    sr.save_store(store, _proven())
    json.dump(drift(0, "DECAY"), open(dr, "w"))
    assert sr.main(["--evidence", ev, "--drift", dr, "--store", store, "--today", "2026-11-30"]) == 1          # evidence 12 days old: nothing used
    json.dump({"generated_at": "2026-11-16T00:00:00+00:00", "groups": {"strategy": {}}}, open(ev, "w"))
    open(dr, "w").write("{broken")
    assert sr.main(["--evidence", ev, "--drift", dr, "--store", store, "--today", "2026-11-16"]) == 1


# ---------------------------------------------------------------------------------------------------- round 563: the drift streak counts only days with new closed trades
def drift_n(i, state, n, key=KEY):
    d = drift(i, state, key)
    d["strategies"][key]["n"] = n
    return d


def test_decay_days_without_new_trades_do_not_move_proven_to_watch():
    s = _proven()
    for i in range(5):
        assert sr.apply_drift(s, drift_n(i, "DECAY", 60), "2026-11-16") == []       # the same 60 trades, five days
    assert s["strategies"][KEY]["status"] == "proven" and s["strategies"][KEY]["drift_streak"]["days"] == 1


def test_decay_days_with_new_trades_still_move_proven_to_watch():
    s = _proven()
    assert sr.apply_drift(s, drift_n(0, "DECAY", 60), "x") == [] and sr.apply_drift(s, drift_n(1, "DECAY", 62), "x") == []
    ch = sr.apply_drift(s, drift_n(2, "DECAY", 65), "x")
    assert ch and ch[0]["to"] == "watch"


def test_a_quiet_day_does_not_reset_a_decay_count():
    s = _proven()
    sr.apply_drift(s, drift_n(0, "DECAY", 60), "x"); sr.apply_drift(s, drift_n(1, "DECAY", 62), "x")
    sr.apply_drift(s, drift_n(2, "STABLE", 62), "x")                                # no new trades: not read at all
    assert s["strategies"][KEY]["drift_streak"]["days"] == 2
    assert sr.apply_drift(s, drift_n(3, "DECAY", 64), "x")[0]["to"] == "watch"


def test_round566_cell_carries_the_newest_closed_time_of_usable_rows():
    rows = [{"net_pnl": 1.0, "entry_price": 100, "quantity": 1, "closed_at": f"2026-11-{10 + i:02d}T10:00:00"} for i in range(5)]
    rows.append({"net_pnl": 1.0, "entry_price": 0, "quantity": 1, "closed_at": "2026-11-30T10:00:00"})   # no readable return: ignored
    c = dm.cell(rows, 20, 30)
    assert c["n"] == 5 and c["last_closed"] == "2026-11-14T10:00:00"
    assert dm.cell([], 20, 30)["last_closed"] is None
