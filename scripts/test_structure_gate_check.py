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
    # negative drop-one edge = the gate does better WITHOUT the rule -> a note, never a FAIL by itself
    r = g.check_ab(_ab(rules={"room": {"drop_one_edge_pp": -0.9, "drop_one_n": 100}}))
    assert r["status"] == g.PASS and any("room" in x for x in r["reasons"])


def test_ab_rule_note_names_only_the_harmful_rule():
    # group424 regression: seed 11 had bearish +0.07 (helps) and rr -0.19 (hurts); only rr may be named
    r = g.check_ab(_ab(edge=0.0, rules={"bearish": {"drop_one_edge_pp": 0.07, "drop_one_n": 100},
                                         "rr": {"drop_one_edge_pp": -0.19, "drop_one_n": 100}}))
    txt = " ".join(r["reasons"])
    assert "rr" in txt and "bearish" not in txt
    assert g.check_ab(_ab(rules={"bearish": {"drop_one_edge_pp": 0.5, "drop_one_n": 100}}))["reasons"] == []


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


# ── group 434: G3 (stability) ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _abx(verdict="pass", edge=0.5, first=0.4, second=0.6, regimes=None, rules=None):
    d = _ab(verdict, 60.0, edge, regimes, rules)
    d["halves"] = {"first": first, "second": second}
    return d


def _r(e, n=100):
    return {"drop_one_edge_pp": e, "drop_one_n": n}


def test_g3_no_input_and_one_seed_is_not_enough_data():
    assert g.check_g3([])["status"] == g.NOINPUT
    assert g.check_g3([_abx()])["status"] == g.NED
    assert g.check_g3([_abx(), _abx("no_data")])["status"] == g.NED


def test_g3_passes_when_everything_agrees():
    r = g.check_g3([_abx(), _abx(), _abx()])
    assert r["status"] == g.PASS and r["rules"] == []


def test_g3_a_seed_that_does_not_pass_fails():
    r = g.check_g3([_abx(), _abx("no_edge", edge=0.1)])
    assert r["status"] == g.FAIL and "not `pass`" in " ".join(r["reasons"])


def test_g3_non_positive_edge_and_a_negative_half_fail():
    assert g.check_g3([_abx(), _abx(edge=-0.1)])["status"] == g.FAIL
    r = g.check_g3([_abx(), _abx(first=-0.2)])
    assert r["status"] == g.FAIL and "half" in " ".join(r["reasons"])


def test_g3_missing_halves_is_a_note_not_a_failure():
    r = g.check_g3([_ab(), _ab()])
    assert r["status"] == g.PASS and any("half stability was not checked" in x for x in r["reasons"])


def test_g3_regime_sign_flip_between_seeds_fails():
    a = _abx(regimes={"bear": {"candidate_n": 80, "edge_pp": 0.4}})
    b = _abx(regimes={"bear": {"candidate_n": 80, "edge_pp": -0.2}})
    r = g.check_g3([a, b], ["s7", "s11"])
    assert r["status"] == g.FAIL and "bear" in " ".join(r["reasons"]) and "s7 +0.40" in " ".join(r["reasons"])
    small = _abx(regimes={"bear": {"candidate_n": 5, "edge_pp": -0.2}})
    assert g.check_g3([a, small])["status"] == g.PASS         # a regime with fewer than 30 candidates is ignored


def test_g3_rule_stability_catches_the_424b_case():
    # chase: +0.07 and 0.00 on two seeds would be "non-negative on both"; a third seed at -0.30 makes it unstable
    seeds = [_abx(rules={"chase": _r(0.07), "bearish": _r(0.2), "rr": _r(-0.2)}),
             _abx(rules={"chase": _r(0.0), "bearish": _r(0.3), "rr": _r(-0.3)}),
             _abx(rules={"chase": _r(-0.30), "bearish": _r(0.1), "rr": _r(-0.32)})]
    by = {x["rule"]: x["verdict"] for x in g.rule_stability(seeds, ["7", "11", "13"])}
    assert by == {"bearish": "earns", "chase": "unstable", "rr": "hurts"}
    r = g.check_g3(seeds, ["7", "11", "13"])
    txt = " ".join(r["reasons"])
    assert r["status"] == g.FAIL and "`chase` flips sign" in txt and "`rr` hurts" in txt and "`bearish`" not in txt


def test_g3_neutral_rule_is_a_note_and_small_samples_are_not_counted():
    seeds = [_abx(rules={"room": _r(0.01), "stretch": _r(0.4, n=5)}), _abx(rules={"room": _r(-0.02), "stretch": _r(-0.4, n=5)})]
    by = {x["rule"]: x["verdict"] for x in g.rule_stability(seeds)}
    assert by == {"room": "neutral", "stretch": "not_enough"}
    r = g.check_g3(seeds)
    assert r["status"] == g.PASS and any("`room` shows no measurable effect" in x for x in r["reasons"])


def test_g3_is_in_the_readout_and_strict_still_works(capsys):
    with tempfile.TemporaryDirectory() as t:
        a, b = (os.path.join(t, n) for n in ("a.json", "b.json"))
        open(a, "w").write(json.dumps(_abx(rules={"rr": _r(-0.3)})))
        open(b, "w").write(json.dumps(_abx(rules={"rr": _r(-0.4)})))
        assert g.main(["--ab", a, "--ab", b, "--strict"]) == 1
    out = capsys.readouterr().out
    assert "G3: FAIL" in out and "rule stability" in out and "rr" in out


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
