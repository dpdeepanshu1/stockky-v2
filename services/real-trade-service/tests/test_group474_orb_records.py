"""tests/test_group474_orb_records.py - opening-range shadow fields on trade records (group 474). Pure helpers."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest  # noqa: F401  (conftest needs it)

try:
    import trade_records as tr
except Exception:  # sqlalchemy missing in a bare sandbox: CI has it
    tr = None


def test_orb_fields_variants():
    if tr is None:
        return
    assert tr._orb_fields(None)["orb_would_enter"] is None
    assert tr._orb_fields("x")["orb_would_enter"] is None
    a = tr._orb_fields({"would_enter": True, "reasons": [], "range_pct": 1.2})
    assert a == {"orb_would_enter": True, "orb_reasons": None, "orb_range_pct": 1.2}
    b = tr._orb_fields({"would_enter": False, "reasons": ["below_vwap", "wide_spread"]})
    assert b["orb_would_enter"] is False and b["orb_reasons"] == "below_vwap"
    c = tr._orb_fields({"would_enter": False, "reason": "no_range"})
    assert c["orb_reasons"] == "no_range"


def test_orb_bucket():
    if tr is None:
        return
    assert tr._orb_bucket({}) == "no_verdict"
    assert tr._orb_bucket({"orb_would_enter": True}) == "orb_would_enter"
    assert tr._orb_bucket({"orb_would_enter": False, "orb_reasons": "below_vwap"}) == "orb_would_skip:below_vwap"
