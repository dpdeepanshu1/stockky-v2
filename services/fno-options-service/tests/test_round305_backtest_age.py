"""Round 305: a saved rolling backtest older than MAX_BACKTEST_AGE_DAYS no longer keeps 'Rolling backtest passed' green."""
from __future__ import annotations

import json
import os

from fno import readiness
from fno.params import Params

NOW = 1_800_000_000.0
DAY = 86400.0
GOOD = {"trades": 45, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}


def test_a_fresh_pass_is_green_and_an_old_pass_is_red_with_the_reason():
    assert readiness.backtest_check({**GOOD, "mtime": NOW - 3 * DAY}, NOW)[0] is True
    ok, why = readiness.backtest_check({**GOOD, "mtime": NOW - 20 * DAY}, NOW)
    assert ok is False and "20 days ago" in why and "run it again" in why
    assert readiness.backtest_check({**GOOD, "mtime": NOW - 14 * DAY}, NOW)[0] is True          # the limit itself is still fine


def test_without_a_clock_or_a_save_time_the_rule_is_as_before():
    assert readiness.backtest_check({**GOOD, "mtime": NOW - 90 * DAY})[0] is True
    assert readiness.backtest_check(GOOD, NOW)[0] is True


def test_latest_rolling_carries_the_save_time_and_evaluate_uses_it(tmp_path):
    d = tmp_path / "backtests"
    d.mkdir()
    f = d / "fno_rolling_nifty_20261001_100000.json"
    f.write_text(json.dumps({"kind": "ROLLING", "strategy": "CORE", "underlying": "NIFTY", "metrics": {"overall": {"trades": 45, "net": 9000}},
                             "verdict": {"status": "PASSED_THIS_TEST_ONLY"}, "data_quality": {"ok": True}}), encoding="utf-8")
    os.utime(f, (NOW - 30 * DAY, NOW - 30 * DAY))
    row = readiness.latest_rolling(str(tmp_path))[("CORE", "NIFTY")]
    assert row["mtime"] == NOW - 30 * DAY
    out = readiness.evaluate({"now": NOW, "backtests": {"CORE": {"NIFTY": row}}})
    item = next(i for i in out["items"] if i["key"] == "backtest_core")
    assert item["ok"] is False and "30 days ago" in item["detail"]
