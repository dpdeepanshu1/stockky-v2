"""Round 303: the service refuses a simulation of two indices (it used to run the first and name the file for both), and the Backtests page offers every strategy the service knows."""
from __future__ import annotations

import os
import re

import pytest

from backtest import report
from fno import strategies

ORDER = list(strategies.ORDER)


def test_a_good_request_has_no_problem():
    assert report.request_problem("sim", ["nifty"], "core", None, ORDER) is None
    assert report.request_problem("sim", ["NIFTY"], "ALL", None, ORDER) is None
    assert report.request_problem("replay", ["NIFTY", "SENSEX"], "CORE", ["2026-11-09"], ORDER) is None      # a replay may take both indices


def test_a_simulation_of_two_indices_is_refused_not_cut_to_the_first():
    msg = report.request_problem("sim", ["NIFTY", "SENSEX"], "CORE", None, ORDER)
    assert msg and "one index per job" in msg
    assert report.request_problem("sim", ["NIFTY", "NIFTY"], "CORE", None, ORDER) is None               # the same index twice is still one index


def test_the_other_refusals_keep_their_messages():
    assert report.request_problem("zzz", ["NIFTY"], "CORE", None, ORDER) == "kind must be replay or sim"
    assert "YYYY-MM-DD" in report.request_problem("replay", ["NIFTY"], "CORE", ["../etc"], ORDER)
    assert report.request_problem("sim", [], "CORE", None, ORDER) == "underlyings must be NIFTY and/or SENSEX"
    assert report.request_problem("sim", ["BANKNIFTY"], "CORE", None, ORDER) == "underlyings must be NIFTY and/or SENSEX"
    assert "strategy must be ALL or one of" in report.request_problem("sim", ["NIFTY"], "NOPE", None, ORDER)


def test_the_backtests_page_lists_every_strategy_in_the_same_order():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "fnoView.ts")
    if not os.path.exists(path):
        pytest.skip("the frontend is not mounted in this service's container")
    src = open(path, encoding="utf-8").read()
    m = re.search(r"export const BT_STRATEGIES: string\[\] = \[(.*?)\];", src, re.S)
    assert m, "BT_STRATEGIES is missing from frontend/src/fnoView.ts"
    assert re.findall(r'"([A-Z_]+)"', m.group(1)) == ORDER
