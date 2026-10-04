"""
tests/test_angelone_feed_unresolved_names.py

Group 124: the AngelOne feed's "resolved 248/250 requested symbols" warning now names
the symbols that got no token. Pure-helper tests; no network, no AngelOne credentials.

Run from services/market-data-service:
    python -m pytest tests/test_angelone_feed_unresolved_names.py -v
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture()
def feed(monkeypatch):
    mh = types.ModuleType("market_hours")
    mh.is_feed_window_ist = lambda: False
    monkeypatch.setitem(sys.modules, "market_hours", mh)
    sys.modules.pop("angelone_ws_feed", None)
    import angelone_ws_feed as f
    yield f
    sys.modules.pop("angelone_ws_feed", None)


def test_names_the_missing_symbols_sorted(feed):
    out = feed._unresolved_suffix(["TCS", "ANNAPURNA", "AAKASH"], {"TCS": "1"})
    assert out == "; unresolved: AAKASH, ANNAPURNA"


def test_suffix_and_case_are_normalised_like_the_scrip_master(feed):
    assert feed._unresolved_suffix(["tcs.ns", "INFY.BO"], {"TCS": "1", "INFY": "2"}) == ""


def test_nothing_missing_gives_empty_string(feed):
    assert feed._unresolved_suffix(["A", "B"], {"A": "1", "B": "2"}) == ""


def test_long_lists_are_capped_with_a_count(feed):
    syms = [f"S{i:03d}" for i in range(25)]
    out = feed._unresolved_suffix(syms, {})
    assert out.endswith("(+5 more)")
    assert "S019" in out and "S020" not in out


def test_bad_input_never_raises(feed):
    assert feed._unresolved_suffix(None, {}) == ""
    assert feed._unresolved_suffix([None, ""], {}) == ""
