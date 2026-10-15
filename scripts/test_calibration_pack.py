"""Tests for scripts/calibration_pack.py (plain asserts; also collected by pytest)."""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibration_pack as c  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wf(picks, edge=0.6, stable=True, passed=150, rejected=300):
    return {"rule": "x", "folds": [{"fold": i + 1, "threshold": p, "test_edge": 0.5} for i, p in enumerate(picks)], "oos_edge": edge,
            "oos_passed_n": passed, "oos_rejected_n": rejected, "stable": stable}


def _bt(**rules):
    base = {r: {"rule": r, "folds": [], "oos_edge": None, "oos_passed_n": 0, "oos_rejected_n": 0, "stable": None, "note": "insufficient_sample"} for r in c.RULE_MAP}
    base.update(rules)
    return {"symbols": 400, "with_outcome": 9000, "horizon": 10, "entry": "next_open", "cost_pct": 0.3, "slip_pct": 0.1, "walk_forward": base}


def _by_rule(bt, **kw):
    return {i["rule"]: i for i in c.suggestions(bt, **kw)}


def test_stable_rule_with_enough_rows_is_suggested_with_both_env_names():
    s = _by_rule(_bt(chase=_wf([1.0, 1.0, 1.0, 1.0])))["chase"]
    assert s["status"] == "suggest" and s["pick"] == 1.0
    txt = c.render(_bt(chase=_wf([1.0, 1.0, 1.0, 1.0])), [])
    assert "STRUCTURE_MAX_CHASE_ATR=1 (now 1.5)" in txt and "PS_STRUCTURE_MAX_CHASE_ATR=1 (now 4)" in txt


def test_rr_has_no_position_stocks_line():
    txt = c.render(_bt(rr=_wf([2.0, 2.0, 2.0, 2.0])), [])
    assert "STRUCTURE_MIN_RR=2 (now 1.2)" in txt and "PS_STRUCTURE_MIN_RR" not in txt


def test_noise_never_produces_a_suggestion():
    rows = _by_rule(_bt(chase=_wf([1.0] * 4, passed=28, rejected=373), stretch=_wf([2.0] * 4, stable=False),
                        room=_wf([1.0] * 4, edge=0.1), rr=_wf([2.0] * 4, edge=None)))
    assert rows["chase"]["status"] == "sample_too_small"
    assert rows["stretch"]["status"] == "unstable"
    assert rows["room"]["status"] == "no_edge"
    assert rows["rr"]["status"] == "insufficient_sample"


def test_pick_equal_to_current_default_is_keep():
    assert _by_rule(_bt(chase=_wf([1.5, 1.5, 1.5, 1.5])))["chase"]["status"] == "keep"


def test_tie_goes_to_the_later_fold():
    assert c._mode([1.0, 1.0, 1.5, 1.5]) == 1.5 and c._mode([]) is None
    assert c._mode([0.5, 1.5, 3.0, 3.0]) == 3.0


def test_missing_rule_and_bad_input_do_not_raise():
    assert all(i["status"] == "no_input" for i in c.suggestions({}))
    assert all(i["status"] == "no_input" for i in c.suggestions(None))
    assert _by_rule({"walk_forward": {}})["room"]["status"] == "no_input"


def test_ab_verdict_header():
    assert "all `pass`" in c.render(_bt(), [{"verdict": "pass"}, {"verdict": "pass"}])
    assert "not all `pass`" in c.render(_bt(), [{"verdict": "pass"}, {"verdict": "no_edge"}])
    assert "none given" in c.render(_bt(), [])


def test_thresholds_floors_are_not_shared_with_params_json():
    assert "STRUCTURE_PARAMS_JSON is not touched" in c.render(_bt(), [])


def test_current_defaults_match_the_service_config():
    """The 'now' values printed by the pack must be the real defaults; fail loudly if a config default moves."""
    rt = open(os.path.join(ROOT, "services", "real-trade-service", "config.py")).read()
    ps = open(os.path.join(ROOT, "services", "position-stocks-service", "config.py")).read()
    for rule, (rt_env, rt_now, ps_env, ps_now) in c.RULE_MAP.items():
        m = re.search(rf'^{rt_env}\s*=\s*_og_float\("{rt_env}",\s*([0-9.]+)\)', rt, re.M)
        assert m and float(m.group(1)) == rt_now, rt_env
        if ps_env:
            name = ps_env[len("PS_"):]
            m = re.search(rf'^{name}\s*=.*_ps\("{name}"\),\s*([0-9.]+)\)\)', ps, re.M)
            assert m and float(m.group(1)) == ps_now, ps_env


def test_cli_writes_out_file():
    with tempfile.TemporaryDirectory() as d:
        bt, out = os.path.join(d, "bt.json"), os.path.join(d, "pack.md")
        json.dump(_bt(chase=_wf([1.0] * 4)), open(bt, "w"))
        assert c.main(["--backtest", bt, "--out", out]) == 0
        assert "SUGGEST 1" in open(out).read()
