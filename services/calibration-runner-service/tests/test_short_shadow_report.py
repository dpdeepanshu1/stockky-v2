"""E98 P0 item 7b: scripts/short_shadow_report.py (offline, fake endpoint) plus the runner job wiring. Plain asserts."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import jobs as J  # noqa: E402
import short_shadow_report as S  # noqa: E402


def doc(**kw):
    d = {"mode": "shadow", "total": 130, "open": 2, "closed": 120, "target": 50, "stop": 60, "eod": 10, "expired": 8, "net_pnl": 340.5,
         "gross_pnl": 500.0, "win_rate_pct": 45.0, "days_with_results": 25}
    d.update(kw)
    return d


def fetch_of(body, code=200):
    return lambda url: (code, body)


def test_pass_when_enough_data_and_net_positive():
    v, reason, lines = S.evaluate(doc())
    assert v == "PASS" and "340.50" in reason and any("counters agree" in l for l in lines)


def test_fail_when_enough_data_and_net_not_positive():
    for net in (-12.0, 0.0):
        assert S.evaluate(doc(net_pnl=net))[0] == "FAIL"


def test_not_enough_data_by_trades_or_by_days():
    v, reason, _ = S.evaluate(doc(closed=40, target=20, stop=15, eod=5))
    assert v == "NOT_ENOUGH_DATA" and "40 of 100" in reason
    assert S.evaluate(doc(days_with_results=7))[0] == "NOT_ENOUGH_DATA"


def test_counter_disagreement_is_a_fail_even_without_enough_data():
    assert S.evaluate(doc(closed=40, target=20, stop=15, eod=4))[0] == "FAIL"        # 39 != 40
    assert S.evaluate(doc(total=100, open=50, closed=120))[0] == "FAIL"              # more rows than total
    v, reason, _ = S.evaluate(doc(net_pnl="n/a"))
    assert v == "FAIL" and "net_pnl" in reason
    assert S.evaluate(doc(stop=None))[0] == "FAIL"


def test_unusable_answers_fail():
    assert S.evaluate(None)[0] == "FAIL" and S.evaluate([1, 2])[0] == "FAIL"
    rc, out = S.run(fetch_of(None, 503), "http://x")
    assert rc == 1 and "HTTP 503" in out and out.endswith("SHORT SHADOW: FAIL - GET /short/shadow returned HTTP 503")
    assert S.run(fetch_of(None, 0), "http://x")[0] == 1


def test_exit_codes_and_last_line():
    rc, out = S.run(fetch_of(doc()), "http://x/")
    assert rc == 0 and out.splitlines()[-1].startswith("SHORT SHADOW: PASS")
    rc, out = S.run(fetch_of(doc(closed=10, target=5, stop=5, eod=0)), "http://x")
    assert rc == 0 and out.splitlines()[-1].startswith("SHORT SHADOW: NOT_ENOUGH_DATA")
    rc, out = S.run(fetch_of(doc(net_pnl=-5)), "http://x")
    assert rc == 1 and out.splitlines()[-1].startswith("SHORT SHADOW: FAIL")


def test_thresholds_are_parameters():
    assert S.evaluate(doc(closed=40, target=20, stop=15, eod=5, days_with_results=10), min_trades=30, min_days=5)[0] == "PASS"
    assert S.main(["--min-trades", "0"]) == 2


def test_job_is_registered_read_only_with_ps_url_and_attention():
    j = J.JOBS["short_shadow_report"]
    steps = j["builder"]({"python": "py", "scripts": "/app/scripts", "rt": "http://rt", "ps": "http://ps"}, {})
    argv = steps[0]["argv"]
    assert len(steps) == 1 and argv[1].endswith("short_shadow_report.py") and argv[argv.index("--ps-url") + 1] == "http://ps"
    assert argv[argv.index("--min-trades") + 1] == "100" and argv[argv.index("--min-days") + 1] == "20"
    assert steps[0]["env"] == {"TOKEN": "{token}"} and list(steps[0]["attention"]) == [1]
    assert j["group"] == "weekly" and not j["heavy"] and j["window"] == "any"
    assert (REPO / "scripts" / "short_shadow_report.py").exists()
