"""
tests/test_reconcile_leg_price.py — offline unit tests for
orders/reconcile.py::_extract_leg_price() (session72 issue #31).

Verifies the full fallback chain:
  1. averageTradedPrice on the leg            → primary
  2. tradedPrice / avgPrice / avgTradedPrice  → secondary fields
  3. leg["price"]                             → static trigger (logged as refutation)
  4. parent_row["averageTradedPrice"]         → parent-row fallback
  5. own_fallback_price                       → last-resort (trigger price, not 0.0)
  6. 0.0                                      → only if caller passes own_fallback_price=0

Also verifies that non-numeric junk in any field is skipped, not raised.

Run from services/position-stocks-service:
    python -m pytest tests/test_reconcile_leg_price.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from orders.reconcile import _extract_leg_price


# ── helpers ──────────────────────────────────────────────────────────────────

def _leg(**kwargs) -> dict:
    return dict(kwargs)


def _parent(**kwargs) -> dict:
    return dict(kwargs)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_averageTradedPrice_on_leg_is_primary():
    leg = _leg(averageTradedPrice=152.75, tradedPrice=100.0, price=140.0)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 152.75


def test_tradedPrice_used_when_averageTradedPrice_absent():
    leg = _leg(tradedPrice=148.50)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 148.50


def test_avgPrice_used_as_tertiary():
    leg = _leg(avgPrice=161.0)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 161.0


def test_avgTradedPrice_used_as_quaternary():
    leg = _leg(avgTradedPrice="177.25")   # string form — Dhan may return strings
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 177.25


def test_leg_price_used_when_no_traded_price():
    """leg['price'] is the static trigger price — a valid last resort before parent."""
    leg = _leg(price=145.0)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 145.0


def test_parent_averageTradedPrice_used_when_leg_has_nothing():
    leg = _leg()
    parent = _parent(averageTradedPrice=155.0)
    assert _extract_leg_price(leg, parent, own_fallback_price=130.0) == 155.0


def test_own_fallback_used_when_all_fields_absent():
    """No fields on leg or parent → own_fallback_price, never 0.0."""
    result = _extract_leg_price(_leg(), _parent(), own_fallback_price=142.50)
    assert result == 142.50


def test_returns_zero_only_when_caller_passes_zero_fallback():
    """The absolute last resort: caller explicitly passes 0.0 (should not happen in prod)."""
    result = _extract_leg_price(_leg(), _parent(), own_fallback_price=0.0)
    assert result == 0.0


def test_non_numeric_traded_price_is_skipped_not_raised():
    """Junk string must not propagate a ValueError — fall through to next field."""
    leg = _leg(averageTradedPrice="N/A", tradedPrice="--", price=138.0)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 138.0


def test_zero_traded_price_is_skipped_falsy():
    """A zero value is falsy → treated as absent → fallback to next field."""
    leg = _leg(averageTradedPrice=0, price=141.0)
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 141.0


def test_string_numeric_traded_price_parsed():
    """Dhan often returns prices as strings — must parse correctly."""
    leg = _leg(averageTradedPrice="183.60")
    assert _extract_leg_price(leg, _parent(), own_fallback_price=130.0) == 183.60


def test_priority_order_is_enforced():
    """averageTradedPrice beats every other field."""
    leg = _leg(
        averageTradedPrice=200.0,
        tradedPrice=100.0,
        avgPrice=110.0,
        avgTradedPrice=120.0,
        price=130.0,
    )
    parent = _parent(averageTradedPrice=140.0)
    assert _extract_leg_price(leg, parent, own_fallback_price=150.0) == 200.0


def test_parent_fallback_beats_own_fallback():
    """parent_row averageTradedPrice ranks above own_fallback_price."""
    leg = _leg()   # nothing on the leg itself
    parent = _parent(averageTradedPrice=160.0)
    assert _extract_leg_price(leg, parent, own_fallback_price=130.0) == 160.0


def test_non_numeric_parent_price_falls_through_to_own_fallback():
    leg = _leg()
    parent = _parent(averageTradedPrice="INVALID")
    assert _extract_leg_price(leg, parent, own_fallback_price=132.0) == 132.0
