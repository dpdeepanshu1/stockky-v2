"""Round 306: the pipeline's Reconcile stage is no longer green for a check of any age."""
from __future__ import annotations

from fno import readiness, stages

NOW = 1_800_000_000.0
DAY = lambda ts: "d"      # noqa: E731  (every timestamp is the same day)


def _rec(age_s, ok=True, **kw):
    inp = {"now": NOW, "enabled": True, "market_open": True, "md_ok": True, "vix_ok": True, "expiries_ok": True, "last_tick": {"ts": NOW - 5, "ok": True, "summary": {"decisions": {}}},
           "events": {}, "open_positions": 0, "broker": True, "reconcile": {"ts": NOW - age_s, "ok": ok, "differences": []}}
    inp.update(kw)
    return {s["key"]: s for s in stages.build(inp, DAY)}["reconcile"]


def test_a_fresh_clean_reconcile_is_ok():
    assert _rec(60)["status"] == "OK"


def test_an_old_clean_reconcile_is_not_green():
    s = _rec(3600)
    assert s["status"] == "idle" and "60 min old" in s["detail"]


def test_an_old_reconcile_with_open_real_positions_in_market_hours_is_a_problem():
    s = _rec(3600, open_positions=1, open_real_positions=1)
    assert s["status"] == "PROBLEM" and "1 REAL position(s) are open" in s["detail"]
    assert _rec(3600, open_positions=1, open_real_positions=1, market_open=False)["status"] == "idle"   # after the close nothing runs, so no alarm


def test_open_demo_positions_do_not_raise_the_alarm():
    assert _rec(3600, open_positions=2, open_real_positions=0)["status"] == "idle"        # the engine reconciles only for REAL positions


def test_without_the_real_count_all_open_positions_count():
    assert _rec(3600, open_positions=1)["status"] == "PROBLEM"


def test_a_difference_stays_red_whatever_its_age():
    assert _rec(3600, ok=False)["status"] == "PROBLEM"


def test_the_limit_is_the_same_as_the_readiness_list():
    assert stages.RECONCILE_MAX_AGE_S == readiness.RECONCILE_MAX_AGE_S
