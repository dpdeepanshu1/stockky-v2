"""Part 1 step 7: regime_learner.py (offline, plain asserts). It must PROPOSE only, one dial, behind a sample minimum, a trial-deflated bar and a two-run rule."""
from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import jobs as J  # noqa: E402
import regime_learner as L  # noqa: E402
import regime_shadow_report as S  # noqa: E402
from test_regime_shadow_report import _days, _log, _rec  # noqa: E402

TODAY = "2026-11-06"


def _run(log, recs, history="", today=TODAY):
    d = tempfile.mkdtemp()
    lp, rp = Path(d, "log.jsonl"), Path(d, "rec.json")
    lp.write_text("\n".join(json.dumps(r) for r in log) + "\n")
    rp.write_text(json.dumps({"records": recs}))
    argv = ["--log", str(lp), str(rp), "--out", d, "--today", today] + (["--history", history] if history else [])
    code = L.main(argv)
    return code, json.loads(Path(d, "regime_learner.json").read_text()) if Path(d, "regime_learner.json").exists() else None


def _winning_late_stressed(n_days=30, seed=1):
    """Stressed days (profile stops entries after 13:00). Late 13:30 trades win steadily, mid-day trades are noise around zero. 3 trades a day."""
    rnd, days, recs, i = random.Random(seed), _days(n_days), [], 0
    for d in days:
        recs.append(_rec(d, "11:00", rnd.uniform(-200, 200), i))
        recs.append(_rec(d, "11:30", rnd.uniform(-200, 200), i + 2))
        recs.append(_rec(d, "13:30", 250.0 + rnd.uniform(-60, 60), i + 1))
        i += 3
    return _log(days, "stressed"), recs


def test_too_little_data_is_not_enough_data_and_exit_1():
    days = _days(25)
    log, recs = _log(days, "stressed"), [_rec(d, "11:00", 100.0, i) for i, d in enumerate(days)]      # 25 trades < 90
    code, r = _run(log, recs)
    assert code == 1 and r["verdict"] == "NOT_ENOUGH_DATA" and r["proposal"] is None


def test_a_steady_winning_blocked_bucket_proposes_one_capped_loosening():
    log, recs = _winning_late_stressed()
    code, r = _run(log, recs)
    assert code == 0 and r["verdict"] == "PROPOSE_LOOSEN", r
    p = r["proposal"]
    assert (p["state"], p["dial"], p["from"], p["to"]) == ("stressed", "ENTRY_LAST_TIME_IST", "13:00", "13:30")      # one 30-minute step
    assert p["confirmed"] is False                                    # first run: never confirmed


def test_loosening_never_goes_beyond_the_all_states_default():
    m = L.BUCKET_DIAL["late"][1]
    assert S._hm(m) == 14 * 60 + 30
    # choppy is at 13:30; a step gives 14:00, a second at 14:00 gives 14:30, a third would be capped at 14:30
    from regime import profiles as P
    saved = dict(P.PROFILE["choppy"])
    try:
        P.PROFILE["choppy"]["ENTRY_LAST_TIME_IST"] = "14:30"
        assert L.restricted("choppy", "late") is False               # at the default there is nothing left to loosen
    finally:
        P.PROFILE["choppy"].clear()
        P.PROFILE["choppy"].update(saved)


def test_noise_does_not_clear_the_bar():
    rnd, days, recs, i = random.Random(7), _days(30), [], 0
    for d in days:
        recs += [_rec(d, "11:00", rnd.uniform(-300, 300), i), _rec(d, "13:30", rnd.uniform(-300, 300), i + 1), _rec(d, "11:30", rnd.uniform(-300, 300), i + 2)]
        i += 3
    code, r = _run(_log(days, "stressed"), recs)
    assert r["verdict"] == "NO_CHANGE" and r["proposal"] is None, r


def test_group_below_the_minimum_cannot_speak():
    days = _days(25)
    recs, i = [], 0
    for d in days:
        recs.append(_rec(d, "11:00", 10.0, i))
        i += 1
    for k, d in enumerate(days[:20]):                                 # only 20 late trades < 30, however good
        recs.append(_rec(d, "13:30", 900.0, i))
        i += 1
    for d in days:                                                    # padding so the total reaches 90
        recs.append(_rec(d, "10:30", 5.0, i))
        recs.append(_rec(d, "10:45", 5.0, i + 1))
        i += 2
    code, r = _run(_log(days, "stressed"), recs)
    late = [g for g in r["groups"] if g["bucket"] == "late"][0]
    assert late["eligible"] is False and r["proposal"] is None


def test_more_trials_raise_the_bar():
    from statistics import NormalDist
    z1, z6 = NormalDist().inv_cdf(1 - 0.05 / 1), NormalDist().inv_cdf(1 - 0.05 / 6)
    assert z6 > z1
    log, recs = _winning_late_stressed()
    _, r = _run(log, recs)
    assert r["trials"] >= 2 and r["z"] > round(z1, 3)


def test_old_trades_fade():
    s_new = L.weighted_stats([{"w": 1.0, "pnl": 100.0}, {"w": 1.0, "pnl": 100.0}])
    s_mix = L.weighted_stats([{"w": 0.5 ** (360 / L.HALF_LIFE_DAYS), "pnl": -1000.0}, {"w": 1.0, "pnl": 100.0}])
    assert abs(s_new["mean"] - 100.0) < 1e-9 and s_mix["mean"] > 90.0           # a 360-day-old loser barely counts
    assert s_mix["n_eff"] < 1.2


def test_loosening_is_confirmed_only_by_the_same_proposal_on_a_different_day():
    log, recs = _winning_late_stressed()
    hist = str(Path(tempfile.mkdtemp(), "learner_history.jsonl"))
    _, r1 = _run(log, recs, hist, today="2026-11-06")
    assert r1["proposal"]["confirmed"] is False
    _, same_day = _run(log, recs, hist, today="2026-11-06")
    assert same_day["proposal"]["confirmed"] is False                 # the same day does not count as a second run
    _, r2 = _run(log, recs, hist, today="2026-11-13")
    assert r2["proposal"]["confirmed"] is True
    assert len(Path(hist).read_text().splitlines()) == 3


def test_a_different_proposal_resets_the_two_run_rule():
    a = {"action": "LOOSEN", "state": "stressed", "dial": "ENTRY_LAST_TIME_IST", "from": "13:00", "to": "13:30"}
    assert L.same_change(a, dict(a)) and not L.same_change(a, dict(a, to="14:00")) and not L.same_change(a, None)


def test_one_dial_only_and_a_losing_bucket_proposes_tightening_confirmed_at_once():
    rnd, days, recs, i = random.Random(3), _days(30), [], 0
    for d in days:                                                    # normal days: no restriction. Late trades lose steadily
        recs += [_rec(d, "11:00", rnd.uniform(-100, 100), i), _rec(d, "14:00", -300.0 + rnd.uniform(-50, 50), i + 1), _rec(d, "10:00", rnd.uniform(-100, 100), i + 2)]
        i += 3
    _, r = _run(_log(days, "normal"), recs)
    p = r["proposal"]
    assert r["verdict"] == "PROPOSE_TIGHTEN" and p["action"] == "TIGHTEN" and p["dial"] == "ENTRY_LAST_TIME_IST" and p["to"] == "13:00" and p["confirmed"] is True
    assert isinstance(p, dict)                                        # a single proposal, never a list


def test_the_learner_cannot_apply_anything():
    src = (REPO / "scripts" / "regime_learner.py").read_text(encoding="utf-8")
    for bad in ("ControlClient", ".overlay(", ".set(", "/control/", "requests", "httpx", "urllib"):
        assert bad not in src, bad


def test_job_is_registered_reads_the_log_history_and_records_and_is_not_auto():
    j = J.JOBS["regime_learner"]
    steps = j["builder"]({"python": sys.executable, "scripts": str(REPO / "scripts"), "exp": "/data/runner/exports", "run": "/tmp/run"}, {})
    argv = steps[0]["argv"]
    assert argv[1].endswith("regime_learner.py") and "/data/runner/weather/log.jsonl" in argv and "/data/runner/weather/learner_history.jsonl" in argv
    assert "/data/runner/exports/rt_latest.json" in argv and j["needs"] == ["export_records"] and j["auto_elsewhere"] is False


def test_nothing_the_learner_proposes_is_a_calibrator_rule():
    import autocalib as A
    ruled = {r.key for r in A.RULES}
    for k in ("ENTRY_LAST_TIME_IST", "OPENING_ENTRY_NOT_BEFORE_IST", "MARKET_STATE_RISK_MULT", "RISK_MAX_ENTRIES_PER_DAY"):
        assert k not in ruled, f"{k} became a calibrator rule: the learner's proposals would be applied without a human"
