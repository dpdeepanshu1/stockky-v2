"""Tests for scripts/structure_gate_check.py (plain asserts; also collected by pytest)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import structure_gate_check as g  # noqa: E402


def _ab(verdict="pass", kept=60.0, edge=0.5, regimes=None, rules=None):
    return {"verdict": verdict, "kept_pct": kept, "edge_pp": edge, "bootstrap": {"ci90": [0.1, 0.9]},
            "by_regime": regimes or {"bull": {"candidate_n": 100, "edge_pp": 0.4}}, "by_rule": rules or {}}


def _live(days=25, np_=40, nd=40, hp=60.0, hd=50.0, rp=2.0, rd=1.0):
    return {"days_observed": days, "passed_buy_now": {"n_done_20d": np_, "hit_5pct_within_20d_pct": hp, "avg_ret_20": rp},
            "policy_demoted": {"n_done_20d": nd, "hit_5pct_within_20d_pct": hd, "avg_ret_20": rd}}


def test_ab_pass_needs_two_samples():
    assert g.check_g2([_ab()])["status"] == g.NED
    assert g.check_g2([_ab(), _ab()])["status"] == g.PASS


def test_ab_no_edge_and_hurts_fail():
    assert g.check_g2([_ab("no_edge"), _ab()])["status"] == g.FAIL
    assert g.check_g2([_ab("hurts"), _ab()])["status"] == g.FAIL


def test_ab_insufficient_is_not_enough_data():
    assert g.check_ab(_ab("insufficient_sample"))["status"] == g.NED
    assert g.check_ab(_ab("no_data"))["status"] == g.NED


def test_ab_kept_pct_and_regime_fail():
    assert g.check_ab(_ab(kept=20.0))["status"] == g.FAIL
    r = g.check_ab(_ab(regimes={"bear": {"candidate_n": 80, "edge_pp": -0.3}}))
    assert r["status"] == g.FAIL and "bear" in r["reasons"][0]
    assert g.check_ab(_ab(regimes={"bear": {"candidate_n": 5, "edge_pp": -0.3}}))["status"] == g.PASS


def test_ab_rule_note_does_not_fail():
    r = g.check_ab(_ab(rules={"room": {"drop_one_edge_pp": 0.9, "drop_one_n": 100}}))
    assert r["status"] == g.PASS and any("room" in x for x in r["reasons"])


def test_g2_no_input():
    assert g.check_g2([])["status"] == g.NOINPUT


def test_g4_paths():
    assert g.check_g4(None)["status"] == g.NOINPUT
    assert g.check_g4(_live(days=1))["status"] == g.NED
    assert g.check_g4(_live(np_=3))["status"] == g.NED
    assert g.check_g4(_live())["status"] == g.PASS
    assert g.check_g4(_live(hp=40.0))["status"] == g.FAIL
    assert g.check_g4(_live(rp=0.1))["status"] == g.FAIL


def test_g4_prepare_upgrade_note():
    d = _live()
    d["prepare_upgrade"] = {"edge": {"hit_5pct_upgraded_minus_held_pts": -4.0}}
    r = g.check_g4(d)
    assert r["status"] == g.PASS and any("P9" in x for x in r["reasons"])


def test_main_files_and_strict(capsys=None):
    with tempfile.TemporaryDirectory() as t:
        a, b, lv = (os.path.join(t, n) for n in ("a.json", "b.json", "l.json"))
        for p, d in ((a, _ab()), (b, _ab()), (lv, {"report": _live()})):
            open(p, "w").write(json.dumps(d))
        assert g.main(["--ab", a, "--ab", b, "--live", lv, "--strict"]) == 0
        assert g.main(["--ab", a, "--strict"]) == 1        # single sample = not enough data
        assert g.main(["--ab", a]) == 0                     # not strict
        assert g.main(["--strict"]) == 0                    # no input at all is not a failure


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
