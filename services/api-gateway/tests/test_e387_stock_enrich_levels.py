"""E387: a real technical support / resistance replaces the +-3% placeholder the decision route fills in first."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_enrich import apply_tech_fields, fill_placeholder_levels  # noqa: E402

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_support", "bound_routes_l.py"), encoding="utf-8").read()


def test_placeholders_are_filled_and_remembered():
    r = {"close": 100.0, "support": None, "resistance": 130.0}
    assert fill_placeholder_levels(r) == {"support"} and r["support"] == 97.0 and r["resistance"] == 130.0
    assert fill_placeholder_levels({"close": None}) == set() and fill_placeholder_levels({"close": "x"}) == set() and fill_placeholder_levels({}) == set()


def test_real_levels_replace_placeholders_but_not_real_values():
    r = {"close": 100.0, "support": None, "resistance": 120.0, "rsi": None}
    ph = fill_placeholder_levels(r)
    apply_tech_fields(r, {"support": 91.5, "resistance": 111.0, "rsi": 44.0, "close": 99.0}, ph)
    assert r["support"] == 91.5            # placeholder replaced
    assert r["resistance"] == 120.0        # the decision's own value is kept
    assert r["rsi"] == 44.0 and r["close"] == 100.0 and ph == set()


def test_none_levels_from_technical_keep_the_placeholder():
    r = {"close": 100.0}
    ph = fill_placeholder_levels(r)
    apply_tech_fields(r, {"support": None, "resistance": None}, ph)
    assert r["support"] == 97.0 and r["resistance"] == 103.0 and ph == {"support", "resistance"}


def test_route_uses_the_helpers_and_initialises_the_set():
    assert "_placeholder_levels = set()" in _SRC and "fill_placeholder_levels(result)" in _SRC and "apply_tech_fields(result, td, _placeholder_levels)" in _SRC
    assert _SRC.index("_placeholder_levels = set()") < _SRC.index("fill_placeholder_levels(result)") < _SRC.index("apply_tech_fields(")
    assert "round(c * 0.97, 2)" not in _SRC
