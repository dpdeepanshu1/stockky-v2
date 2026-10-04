"""
tests/test_fetch_fundamentals_symbol.py - group112 (log-audit item 4 leftover): prediction/main.py::_fetch_fundamentals
asks market-data for the canonical ".NS" spelling of a plain NSE ticker, and leaves indices, names with spaces and
already-suffixed symbols exactly as given.
Run from services/decision-prediction-service/prediction:  python3 -m pytest tests/test_fetch_fundamentals_symbol.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_PRED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pm():
    added = _PRED_DIR not in sys.path
    if added:
        sys.path.insert(0, _PRED_DIR)
    before = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location("_prediction_main_fundsym_under_test", os.path.join(_PRED_DIR, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if added and _PRED_DIR in sys.path:
            sys.path.remove(_PRED_DIR)
        for k in set(sys.modules) - before:
            if k.startswith("pred_") or k == "_prediction_main_fundsym_under_test":
                sys.modules.pop(k, None)


class _Resp:
    status_code = 200

    def json(self):
        return {"pe_ratio": 20.0, "roe": 0.15}


CASES = [
    ("INFY", "INFY.NS"), ("infy", "INFY.NS"), (" tcs ", "TCS.NS"), ("M&M", "M&M.NS"), ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"),
    ("INFY.NS", "INFY.NS"), ("RELIANCE.BO", "RELIANCE.BO"),
    ("NIFTY", "NIFTY"), ("NIFTY50", "NIFTY50"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX"),
    ("INDIAVIX", "INDIAVIX"), ("^NSEI", "^NSEI"), ("NIFTY BANK", "NIFTY BANK"), ("KFIN TECHNOLOGIES", "KFIN TECHNOLOGIES"),
]


@pytest.mark.parametrize("given, requested", CASES)
def test_helper_canonicalises_only_plain_tickers(pm, given, requested):
    assert pm._md_fundamentals_symbol(given) == requested


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_symbol_is_passed_through(pm, blank):
    assert pm._md_fundamentals_symbol(blank) == blank


@pytest.mark.parametrize("given, requested", CASES)
def test_fetch_requests_the_canonical_url_and_still_normalises_the_payload(pm, monkeypatch, given, requested):
    urls = []
    monkeypatch.setattr(pm.httpx, "get", lambda url, timeout=None: (urls.append(url), _Resp())[1])
    out = pm._fetch_fundamentals(given)
    assert urls == [f"{pm.MARKET_DATA_URL}/fundamentals/{requested}"]
    assert out["pe_ratio"] == 20.0 and out["roe"] == 0.15


def test_a_failed_fetch_still_returns_an_empty_dict(pm, monkeypatch):
    def boom(url, timeout=None):
        raise RuntimeError("down")
    monkeypatch.setattr(pm.httpx, "get", boom)
    assert pm._fetch_fundamentals("INFY") == {}
