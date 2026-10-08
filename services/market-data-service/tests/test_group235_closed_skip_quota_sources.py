"""
group235: with the market closed, a symbol that has no close anywhere (not in the bhavcopy, no cached or last-good
row) no longer walks the quota-limited sources (IndianAPI, TwelveData, AlphaVantage, Polygon). NSE-direct, AngelOne
and Yahoo still run. QUOTE_CLOSED_SKIP_QUOTA_SOURCES=0 restores the old walk.

Run from services/market-data-service:
    python3 -m pytest tests/test_group235_closed_skip_quota_sources.py -v
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main
from test_group233_closed_market_last_close import harness  # noqa: F401  (same closed-market harness)

_NAMES = {
    "nse": "_waterfall_nse_direct_price",
    "angel": "_waterfall_angelone_price",
    "indian": "_waterfall_indianapi_price",
    "twelve": "_waterfall_twelvedata_price",
    "alpha": "_waterfall_alphavantage_price",
    "polygon": "_waterfall_polygon_price",
}


@pytest.fixture()
def calls(monkeypatch):
    seen = []
    ret = {}
    for tag, fn in _NAMES.items():
        monkeypatch.setattr(main, fn, (lambda t: (lambda *a, **k: (seen.append(t), ret.get(t))[1]))(tag))
    monkeypatch.setattr(main, "_in_cooldown", lambda name: False)
    monkeypatch.delenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES", raising=False)
    return seen, ret


def _walk(sym="NOBHAVCO"):
    try:
        return main._get_quote_inner(sym)
    except Exception:  # noqa: BLE001  (a failed quote may raise or return a payload; only the calls matter here)
        return None


def test_closed_unknown_symbol_skips_quota_sources(harness, calls):
    seen, _ret = calls
    _walk()
    assert "nse" in seen and "angel" in seen
    assert not {"indian", "twelve", "alpha", "polygon"} & set(seen)


def test_open_market_still_walks_every_source(harness, calls, monkeypatch):
    seen, _ret = calls
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    _walk()
    assert {"nse", "angel", "indian", "twelve", "alpha", "polygon"} <= set(seen)


def test_env_switch_restores_old_walk(harness, calls, monkeypatch):
    seen, _ret = calls
    monkeypatch.setenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES", "0")
    _walk()
    assert {"indian", "twelve", "alpha", "polygon"} <= set(seen)


def test_nse_direct_price_still_served_while_closed(harness, calls):
    seen, ret = calls
    ret["nse"] = 123.4
    out = _walk()
    assert out and out["price"] == 123.4 and out["source"] == "nse_direct"
    assert not {"indian", "twelve", "alpha", "polygon"} & set(seen)


def test_angelone_price_still_served_while_closed(harness, calls):
    seen, ret = calls
    ret["angel"] = 55.0
    out = _walk()
    assert out and out["price"] == 55.0 and out["source"] == "angelone_rest"


def test_helper_fails_open(monkeypatch):
    monkeypatch.delenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES", raising=False)

    def _boom():
        raise RuntimeError("clock")
    monkeypatch.setattr(main, "_quote_market_closed", _boom)
    assert main._quote_closed_skip_quota_sources() is False


def test_helper_follows_closed_flag_and_switch(monkeypatch):
    monkeypatch.delenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES", raising=False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: True)
    assert main._quote_closed_skip_quota_sources() is True
    monkeypatch.setenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES", "off")
    assert main._quote_closed_skip_quota_sources() is False
    monkeypatch.delenv("QUOTE_CLOSED_SKIP_QUOTA_SOURCES")
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    assert main._quote_closed_skip_quota_sources() is False
