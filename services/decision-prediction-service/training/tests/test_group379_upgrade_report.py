"""group379 (F2): the PREPARE TO BUY -> BUY NOW upgrade gets its own group, a control group and an honest edge in the structure report
(training/structure_obs.py). Plain asserts; runs with pytest or `python3 tests/test_group379_upgrade_report.py`."""
from __future__ import annotations

import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import structure_obs as so  # noqa: E402


def _row(final, legacy, day="2026-10-01", **kw):
    d = {"obs_date": day, "final_decision": final, "legacy_decision": legacy, "eval_status": "done", "ret_5": 1.0, "ret_20": 3.0,
         "hit_5pct_20": 1, "target_before_stop": 1, "mfe_20": 6.0, "mae_20": -2.0, "nifty_ret_20": 1.0, "entry_chase_atr": 0.3}
    d.update(kw)
    return d


UP, HELD, KEPT = ("BUY NOW", "PREPARE TO BUY"), ("PREPARE TO BUY", "PREPARE TO BUY"), ("BUY NOW", "BUY NOW")


def test_groups_are_keyed_on_the_pre_policy_label():
    rows = [_row(*UP), _row(*UP), _row(*HELD), _row(*KEPT), _row(*KEPT), _row(*KEPT),
            _row("WAIT", "PREPARE TO BUY"),           # a PREPARE that fell to WAIT is in none of the three groups
            _row("BUY NOW", "WAIT"),                  # a BUY NOW whose legacy label was WAIT is not an upgrade of a PREPARE
            _row("BUY NOW", None)]                    # no legacy label (sanitize() always sets one): in no group
    pu = so.summarize(rows, min_n=1)["prepare_upgrade"]
    assert pu["upgraded"]["n"] == 2 and pu["held_at_prepare"]["n"] == 1
    assert pu["kept_buy_now"]["n"] == 3


def test_edge_is_none_until_both_groups_have_min_n_done_rows():
    rows = [_row(*UP, hit_5pct_20=1)] * 3 + [_row(*HELD, hit_5pct_20=0)] * 2 + [_row(*KEPT)] * 5
    e = so.summarize(rows, min_n=3)["prepare_upgrade"]["edge"]
    assert e["insufficient_sample_vs_held"] is True        # held has 2 done rows, needs 3
    assert e["insufficient_sample_vs_kept"] is False
    assert e["hit_5pct_upgraded_minus_held_pts"] is None and e["avg_ret_20_upgraded_minus_held_pts"] is None
    assert e["hit_5pct_upgraded_minus_kept_buy_now_pts"] == 0.0     # both groups hit 100%
    assert e["min_n"] == 3


def test_edge_numbers_once_the_sample_is_there():
    rows = ([_row(*UP, hit_5pct_20=1, ret_20=5.0)] * 4 + [_row(*UP, hit_5pct_20=0, ret_20=-1.0)] * 1 +
            [_row(*HELD, hit_5pct_20=1, ret_20=2.0)] * 2 + [_row(*HELD, hit_5pct_20=0, ret_20=0.0)] * 3 +
            [_row(*KEPT, hit_5pct_20=1, ret_20=4.0)] * 5)
    pu = so.summarize(rows, min_n=5)["prepare_upgrade"]
    assert pu["upgraded"]["hit_5pct_within_20d_pct"] == 80.0 and pu["held_at_prepare"]["hit_5pct_within_20d_pct"] == 40.0
    e = pu["edge"]
    assert e["insufficient_sample_vs_held"] is False and e["insufficient_sample_vs_kept"] is False
    assert e["hit_5pct_upgraded_minus_held_pts"] == 40.0
    assert e["avg_ret_20_upgraded_minus_held_pts"] == round(3.8 - 0.8, 3)
    assert e["hit_5pct_upgraded_minus_kept_buy_now_pts"] == -20.0
    assert "verdict" not in e and "confidence interval" in e["note"]


def test_unfinished_rows_do_not_count_toward_the_sample():
    pending = _row(*UP, eval_status="partial", hit_5pct_20=None, ret_20=None, ret_5=None)
    pu = so.summarize([pending] * 10 + [_row(*HELD)] * 10, min_n=5)["prepare_upgrade"]
    assert pu["upgraded"]["n"] == 10 and pu["upgraded"]["n_done_20d"] == 0
    assert pu["edge"]["insufficient_sample_vs_held"] is True and pu["edge"]["hit_5pct_upgraded_minus_held_pts"] is None


def test_empty_and_garbage_input_is_safe():
    for rows in ([], None, [None, "x", 3]):
        s = so.summarize(rows, min_n=30)
        pu = s["prepare_upgrade"]
        assert pu["upgraded"]["n"] == 0 and pu["edge"]["insufficient_sample_vs_held"] is True
        assert pu["edge"]["hit_5pct_upgraded_minus_held_pts"] is None


def test_existing_summary_keys_are_unchanged():
    s = so.summarize([_row("BUY NOW", "BUY NOW"), _row("WAIT", "BUY NOW")], min_n=1)
    for k in ("total", "days_observed", "no_buy_now_days", "by_decision", "passed_buy_now", "policy_demoted", "edge"):
        assert k in s
    assert s["edge"]["hit_5pct_passed_minus_demoted_pts"] == 0.0


def test_report_carries_the_upgrade_section():
    from test_group374_structure_obs import _DB, M, OBS
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "symbol": "UPG", "obs_date": "2026-10-10", "legacy_decision": "PREPARE TO BUY", "final_decision": "BUY NOW"},
                            {**OBS, "symbol": "HLD", "obs_date": "2026-10-10", "legacy_decision": "PREPARE TO BUY", "final_decision": "PREPARE TO BUY"}])
    out = so.report(db, M, days=30, min_n=1, today=date(2026, 10, 15))
    assert out["prepare_upgrade"]["upgraded"]["n"] == 1 and out["prepare_upgrade"]["held_at_prepare"]["n"] == 1


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"ok {n} tests")
