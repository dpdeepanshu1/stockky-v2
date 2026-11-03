"""E285 round 153 (plan item 6): Gate 5.6 weights the target edge by a win chance (COST_GATE_WIN_PROB 0.40) and needs MIN_NET_EDGE_RS net rupees."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import cost_model  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ev(px, q, tgt=1.5, wp=0.40):
    old = config.MIN_NET_EDGE_RS
    config.MIN_NET_EDGE_RS = 3.0
    try:
        return cost_model.evaluate_entry_cost_gate(px, q, tgt, product_type="MIS", win_prob=wp)
    finally:
        config.MIN_NET_EDGE_RS = old


def test_one_share_300_rupee_trade_is_refused_and_a_big_target_passes():
    r = _ev(300.0, 1)
    assert not r.passes and not r.passes_min_net and r.passes_min_ratio
    assert r.weighted_edge == round(r.expected_edge * 0.40, 2) and r.win_prob == 0.40
    big = _ev(500.0, 40)
    assert big.passes and big.ratio >= 3.0


def test_small_target_fails_the_ratio_after_weighting_but_passed_at_full_target():
    full = _ev(500.0, 40, tgt=0.5, wp=1.0)
    wtd = _ev(500.0, 40, tgt=0.5, wp=0.40)
    assert full.passes and not wtd.passes and not wtd.passes_min_ratio
    assert full.weighted_edge == full.expected_edge


def test_net_floor_off_at_zero():
    old = config.MIN_NET_EDGE_RS
    try:
        config.MIN_NET_EDGE_RS = 0.0
        assert cost_model.evaluate_entry_cost_gate(300.0, 1, 1.5, product_type="MIS", win_prob=0.40).passes
    finally:
        config.MIN_NET_EDGE_RS = old


def test_default_comes_from_config_and_junk_means_full_target_in_the_function():
    old = config.COST_GATE_WIN_PROB
    try:
        config.COST_GATE_WIN_PROB = 0.5
        assert cost_model.evaluate_entry_cost_gate(500.0, 10, 1.5, product_type="MIS").win_prob == 0.5
        assert cost_model.evaluate_entry_cost_gate(500.0, 10, 1.5, product_type="MIS", win_prob=7).win_prob == 1.0
        assert cost_model.evaluate_entry_cost_gate(500.0, 10, 1.5, product_type="MIS", win_prob="x").win_prob == 1.0
    finally:
        config.COST_GATE_WIN_PROB = old


def test_config_defaults_and_junk_env_fall_back():
    for var, fn, dflt, cases in (
        ("COST_GATE_WIN_PROB", config._cost_gate_win_prob, 0.40, (("", 0.40), ("0.5", 0.5), ("junk", 0.40), ("0", 0.40), ("2", 0.40), ("1", 1.0))),
        ("MIN_NET_EDGE_RS", config._min_net_edge_rs, 3.0, (("", 3.0), ("5", 5.0), ("junk", 3.0), ("-1", 3.0), ("0", 0.0))),
    ):
        for raw, want in cases:
            os.environ[var] = raw
            try:
                assert fn() == want, (var, raw)
            finally:
                os.environ.pop(var, None)


def test_entry_message_and_catalog_and_protection_are_wired():
    e = open(os.path.join(ROOT, "entry_engine", "entry.py"), encoding="utf-8").read()
    assert "cost_check.weighted_edge" in e and "win chance" in e and "cost gate: qty" in e and "passes_min_net" in e
    cat = open(os.path.join(ROOT, "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("COST_GATE_WIN_PROB"' in cat and 'Flag("MIN_NET_EDGE_RS"' in cat
    auto = open(os.path.join(ROOT, "..", "calibration-runner-service", "autocalib.py"), encoding="utf-8").read()
    assert '"COST_GATE_WIN_PROB":' in auto and '"MIN_NET_EDGE_RS":' in auto


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                f += 1; print("FAIL", n, type(ex).__name__, ex)
    sys.exit(1 if f else 0)
