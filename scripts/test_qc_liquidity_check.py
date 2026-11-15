"""round 562 (E587): the quick check treats 'no bars today' as INFO (exit 3), a present key as OK (0), anything else as WARN (1)."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from qc_liquidity_check import classify  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def test_no_bars_is_info():
    code, line = classify(json.dumps({"detail": "no bars for RELIANCE today (untracked, illiquid or market closed)"}))
    assert code == 3 and "recheck in market hours" in line


def test_key_present_is_ok_with_fields():
    code, line = classify(json.dumps({"symbol": "RELIANCE", "liquidity": {"turnover": {}, "spread": None}}))
    assert code == 0 and "spread" in line and "turnover" in line


def test_key_present_but_null_is_still_ok():
    assert classify(json.dumps({"liquidity": None}))[0] == 0


def test_bars_but_no_key_is_warn():
    code, line = classify(json.dumps({"symbol": "RELIANCE", "price": 1}))
    assert code == 1 and "no liquidity key" in line


def test_other_error_detail_is_warn():
    assert classify(json.dumps({"detail": "Not authenticated"}))[0] == 1


def test_not_json_and_non_dict_are_warn():
    assert classify("")[0] == 1
    assert classify("<html>502</html>")[0] == 1
    assert classify("[1, 2]")[0] == 1


def test_cli_exit_codes():
    def run(body):
        return subprocess.run([sys.executable, os.path.join(HERE, "qc_liquidity_check.py")], input=body, capture_output=True, text=True)
    r = run(json.dumps({"detail": "no bars for X today"}))
    assert r.returncode == 3 and r.stdout.strip()
    assert run(json.dumps({"liquidity": {}})).returncode == 0
    assert run("nope").returncode == 1


def test_script_wires_the_classifier():
    s = open(os.path.join(HERE, "vm_quick_check.sh")).read()
    assert "qc_liquidity_check.py" in s and '3) info "popup liquidity badge' in s
