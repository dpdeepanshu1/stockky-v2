"""Drift guard (group107): the scheduler's two hardcoded copies of the regime constants must show the same
VALUES real-trade-service actually trades with (its config.py defaults).

The scheduler does not import real-trade-service, so the copies drifted: ENTRY_REGIME_MIN_SCORE was lowered
38 -> 25 on 2026-09-03 in real-trade-service but the scheduler kept warning about "38". This test reads
config.py as source and compares values only. It does NOT compare review dates: a review date is the
owner's statement that he reviewed the value, so no test or code change may move it on its own.
(2026-10-04: the owner kept all six values and asked for the dates to be moved to that day; group112+1.)
Dates are checked only for agreement between the copies, never against a fixed day, so the next review
moves them in one place per service and this test says if one copy was missed.
"""
import ast
import os
import re

import scheduler.governance_check as gov

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
RT_CONFIG = os.path.join(SERVICES, "real-trade-service", "config.py")
HYDRATOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scheduler", "weekend_hydrator.py")


def _rt_default(name: str) -> float:
    src = open(RT_CONFIG, encoding="utf-8").read()
    m = re.search(r'^%s\s*=.*?or\s*"([^"]+)"\)' % re.escape(name), src, re.M)
    assert m, f"{name} default not found in real-trade-service/config.py"
    return float(m.group(1))


def _hydrator_constants() -> dict:
    tree = ast.parse(open(HYDRATOR, encoding="utf-8").read())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_check_regime_constant_staleness":
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REGIME_CONSTANTS" for t in node.targets):
                    return {k.value: ast.literal_eval(v)[0] for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("REGIME_CONSTANTS not found in weekend_hydrator._check_regime_constant_staleness")


def test_governance_check_values_match_real_trade_config():
    for name, entry in gov.REGIME_CONSTANTS.items():
        assert float(entry[0]) == _rt_default(name), name


def test_weekend_hydrator_values_match_real_trade_config():
    consts = _hydrator_constants()
    assert consts, "no constants found"
    for name, val in consts.items():
        assert float(val) == _rt_default(name), name


def test_the_guard_reads_the_known_regression_value():
    assert _rt_default("ENTRY_REGIME_MIN_SCORE") == 25.0
    assert float(gov.REGIME_CONSTANTS["ENTRY_REGIME_MIN_SCORE"][0]) == 25.0


RT_ADAPTIVE = os.path.join(SERVICES, "real-trade-service", "adaptive_thresholds.py")


def _rt_review_dates() -> dict:
    """{constant: review date} from real-trade-service/adaptive_thresholds.py::_REGIME_CONSTANTS (read as source)."""
    src = open(RT_ADAPTIVE, encoding="utf-8").read()
    block = re.search(r"_REGIME_CONSTANTS\s*=\s*\{(.*?)\n\}", src, re.S)
    assert block, "_REGIME_CONSTANTS not found in real-trade-service/adaptive_thresholds.py"
    return dict(re.findall(r'"([A-Z0-9_]+)":.*?"(\d{4}-\d{2}-\d{2})"', block.group(1)))


def test_governance_check_review_dates_match_real_trade():
    rt = _rt_review_dates()
    assert rt, "no review dates found"
    for name, entry in gov.REGIME_CONSTANTS.items():
        assert entry[1] == rt[name], f"{name}: scheduler says {entry[1]}, real-trade says {rt[name]}"


def test_weekend_hydrator_review_dates_match_real_trade():
    rt = _rt_review_dates()
    tree = ast.parse(open(HYDRATOR, encoding="utf-8").read())
    found = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_check_regime_constant_staleness":
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REGIME_CONSTANTS" for t in node.targets):
                    found = {k.value: ast.literal_eval(v)[1] for k, v in zip(node.value.keys, node.value.values)}
    assert found, "REGIME_CONSTANTS not found in weekend_hydrator"
    for name, reviewed in found.items():
        assert reviewed == rt[name], f"{name}: hydrator says {reviewed}, real-trade says {rt[name]}"
