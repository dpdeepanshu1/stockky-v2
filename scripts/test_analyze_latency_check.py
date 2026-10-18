"""Tests for scripts/analyze_latency_check.py (group 434)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_latency_check as a  # noqa: E402


def _get(ms, fail=()):
    def g(url, timeout):
        if any(f in url for f in fail):
            raise RuntimeError("down")
        return ms
    return g


def _m(p95, n=40, requests=40, failed=0):
    return {"n": n, "requests": requests, "failed": failed, "p50_ms": p95 / 2, "p95_ms": p95}


def test_percentile():
    assert a.percentile([], 0.5) is None and a.percentile([1.0], 0.95) == 1.0
    assert a.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0 and abs(a.percentile(list(range(101)), 0.95) - 95.0) < 1e-9


def test_first_round_is_a_warmup_and_failures_are_counted():
    m = a.measure("http://x", ["A", "B"], 3, 1.0, _get(10.0, fail=("/B",)))
    assert m["requests"] == 6 and m["failed"] == 3 and m["n"] == 2          # B always fails; A is timed in rounds 2 and 3 only
    assert a.measure("http://x", ["A", "B"], 1, 1.0, _get(10.0))["n"] == 2   # one round: nothing dropped


def test_verdict_pass_fail_and_noise_floor():
    assert a.verdict(_m(110.0), _m(100.0))["status"] == "PASS"
    assert a.verdict(_m(130.0), _m(100.0))["status"] == "FAIL"
    assert a.verdict(_m(6.0), _m(2.0))["status"] == "PASS"                  # +200 % but only 4 ms: under the 5 ms noise floor
    assert a.verdict(_m(8.0), _m(2.0))["status"] == "FAIL"                  # +6 ms and +300 %: real


def test_verdict_does_not_pass_on_too_little_or_too_broken_data():
    assert a.verdict(_m(100.0, n=5), _m(100.0))["status"] == "NOT_ENOUGH_DATA"
    assert a.verdict(_m(100.0), _m(100.0, n=5))["status"] == "NOT_ENOUGH_DATA"
    assert a.verdict(_m(100.0, requests=100, failed=30, n=70), _m(100.0))["status"] == "NOT_ENOUGH_DATA"


def test_cli_save_then_compare(tmp_path, capsys):
    f = str(tmp_path / "b.json")
    syms = ",".join(f"S{i}" for i in range(12))
    assert a.main(["--symbols", syms, "--save", f], get=_get(50.0)) == 0
    assert json.load(open(f))["p95_ms"] == 50.0
    assert a.main(["--symbols", syms, "--compare", f], get=_get(52.0)) == 0
    assert "G1 live latency: PASS" in capsys.readouterr().out
    assert a.main(["--symbols", syms, "--compare", f], get=_get(80.0)) == 1
    assert "FAIL" in capsys.readouterr().out
