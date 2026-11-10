"""
E325 - a NaN / inf live price must never reach the exit engine as a tradable price.

market_feed.get_quote's /quote path guarded with `not price or float(price) <= 0`: NaN passes both tests, so a NaN quote
became a Tick and every `ltp <= stop` comparison for a held stock turned False (stop never fires, no alert). All tick
sources now share feed._price_ok (finite and > 0), and exit.evaluate_mode treats an unusable tick as "no price"
(counted, alerted after a streak) instead of evaluating it.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e325_nan_price_guard.py -q
"""
from __future__ import annotations

import math

import pytest

from market_feed import feed
from exit_engine import exit as ex

NAN, INF = float("nan"), float("inf")
GOOD = [100.0, 0.05, "250.5", 1]
BAD = [None, 0, 0.0, -1.0, NAN, INF, -INF, "nan", "inf", "n/a", "", [1], {}]


@pytest.mark.parametrize("v", GOOD)
def test_feed_price_ok_accepts_real_prices(v):
    assert feed._price_ok(v) is True


@pytest.mark.parametrize("v", BAD)
def test_feed_price_ok_rejects_everything_else(v):
    assert feed._price_ok(v) is False


@pytest.mark.parametrize("v", GOOD)
def test_exit_ltp_usable_accepts_real_prices(v):
    assert ex._ltp_usable(v) is True


@pytest.mark.parametrize("v", BAD)
def test_exit_ltp_usable_rejects_everything_else(v):
    assert ex._ltp_usable(v) is False


def test_the_old_guard_really_let_nan_through():
    """Documents why the fix is needed: the pre-E325 test for the /quote path accepted NaN."""
    price = NAN
    old_rejects = (not price) or float(price) <= 0
    assert old_rejects is False          # NaN was NOT rejected by the old guard
    assert feed._price_ok(price) is False


def test_exit_module_has_math_imported():
    """_ltp_usable needs `math`; a missing import would raise NameError inside the per-position loop."""
    assert hasattr(ex, "math") and ex.math is math
