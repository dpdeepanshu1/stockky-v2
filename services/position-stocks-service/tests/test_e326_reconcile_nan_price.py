"""
E326 - position-stocks reconcile must not book a NaN / inf / negative price as an exit.

`if v:` is True for NaN, so `_extract_leg_price` returned NaN as the exit price (realized P&L NaN, capital release
NaN, daily-loss check False). The EOD flat-SELL path did the same with `float(raw_fill) if raw_fill else None`, and
`_num` let inf through. Zero was already handled (falsy). Unusable values now fall through to the next source.

Run from services/position-stocks-service:
    python -m pytest tests/test_e326_reconcile_nan_price.py -q
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from orders.reconcile import _extract_leg_price, _num

NAN, INF = float("nan"), float("inf")
BAD = [NAN, INF, -INF, -5.0, "nan", "inf"]


@pytest.mark.parametrize("bad", BAD)
def test_bad_leg_traded_price_falls_through_to_the_next_good_key(bad):
    leg = {"averageTradedPrice": bad, "tradedPrice": 101.5}
    assert _extract_leg_price(leg, {}, own_fallback_price=99.0) == 101.5


@pytest.mark.parametrize("bad", BAD)
def test_bad_leg_price_everywhere_uses_the_own_trigger_price(bad):
    leg = {"averageTradedPrice": bad, "tradedPrice": bad, "avgPrice": bad, "avgTradedPrice": bad, "price": bad}
    got = _extract_leg_price(leg, {"averageTradedPrice": bad}, own_fallback_price=99.0)
    assert got == 99.0 and math.isfinite(got)


@pytest.mark.parametrize("bad", BAD)
def test_bad_parent_price_is_not_used(bad):
    assert _extract_leg_price({}, {"averageTradedPrice": bad}, own_fallback_price=97.0) == 97.0


def test_good_prices_unchanged():
    assert _extract_leg_price({"averageTradedPrice": 100.25}, {}, own_fallback_price=99.0) == 100.25
    assert _extract_leg_price({"price": 98.0}, {}, own_fallback_price=99.0) == 98.0


@pytest.mark.parametrize("bad", [NAN, INF, -INF, 0, -1, "x", None])
def test_num_rejects_non_finite_and_non_positive(bad):
    assert _num(bad) is None


def test_num_accepts_a_real_price():
    assert _num("101.5") == 101.5
