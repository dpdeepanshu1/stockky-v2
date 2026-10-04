"""group101 item 23: `_waterfall_nse_direct_price` and `_fetch_nse_fundamentals` share the quote-equity pause.

Group96 paused `bhavcopy.delivery_from_quote` after a 401/403/429. These two callers in main.py still went to
NSE on every call. They now check the same pause and feed it their status.
Run from services/market-data-service:  python3 -m pytest tests/test_nse_quote_pause_shared.py -q
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import bhavcopy as bh
import main as m


class _Client:
    def __init__(self, status, payload=None):
        self.status, self.payload, self.calls = status, payload or {}, 0

    def get(self, url, **kw):
        self.calls += 1
        return types.SimpleNamespace(status_code=self.status, content=b"{}", json=lambda: self.payload)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    bh._QUOTE_BLOCK.update({"until": 0.0, "status": None, "skipped": 0})
    monkeypatch.setattr(m, "_waterfall_equity_base", lambda s: "TESTCO")
    monkeypatch.setattr(m, "_in_cooldown", lambda name: False)
    monkeypatch.setattr(m, "_set_cooldown", lambda *a, **k: None)
    monkeypatch.delenv("NSE_QUOTE_BLOCK_SECONDS", raising=False)
    yield
    bh._QUOTE_BLOCK.update({"until": 0.0, "status": None, "skipped": 0})


def _use(monkeypatch, client):
    monkeypatch.setattr(bh, "_nse_client", lambda: client)


def test_price_403_starts_pause_and_next_calls_skip_nse(monkeypatch):
    c = _Client(403)
    _use(monkeypatch, c)
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert c.calls == 1 and bh.nse_quote_blocked() is True
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert m._fetch_nse_fundamentals("TESTCO") is None
    assert c.calls == 1                         # no further NSE round trips during the pause


def test_fundamentals_403_pauses_the_price_path_too(monkeypatch):
    c = _Client(403)
    _use(monkeypatch, c)
    assert m._fetch_nse_fundamentals("TESTCO") is None
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert bh.delivery_from_quote("TESTCO") is None
    assert c.calls == 1


def test_pause_blocks_all_three_callers_when_started_by_delivery(monkeypatch):
    c = _Client(403)
    _use(monkeypatch, c)
    assert bh.delivery_from_quote("TESTCO") is None
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert m._fetch_nse_fundamentals("TESTCO") is None
    assert c.calls == 1


def test_404_does_not_start_a_pause(monkeypatch):
    c = _Client(404)
    _use(monkeypatch, c)
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert m._waterfall_nse_direct_price("TESTCO") is None
    assert c.calls == 2 and bh.nse_quote_blocked() is False


def test_200_still_returns_price_and_fundamentals(monkeypatch):
    payload = {"priceInfo": {"lastPrice": 101.5}, "industryInfo": {"sector": "IT", "industry": "Software"},
               "info": {}, "securityInfo": {"faceValue": 2}}
    c = _Client(200, payload)
    _use(monkeypatch, c)
    assert m._waterfall_nse_direct_price("TESTCO") == 101.5
    assert m._fetch_nse_fundamentals("TESTCO")["secInfo"]["sector"] == "IT"
    assert c.calls == 2


def test_pause_disabled_with_zero_seconds(monkeypatch):
    monkeypatch.setenv("NSE_QUOTE_BLOCK_SECONDS", "0")
    c = _Client(403)
    _use(monkeypatch, c)
    m._waterfall_nse_direct_price("TESTCO")
    m._waterfall_nse_direct_price("TESTCO")
    assert c.calls == 2


def test_wrappers_never_raise_when_bhavcopy_helpers_are_missing(monkeypatch):
    stub = types.ModuleType("bhavcopy")
    stub._nse_client = lambda: _Client(200, {"priceInfo": {"lastPrice": 5}})
    monkeypatch.setitem(sys.modules, "bhavcopy", stub)
    assert m._nse_quote_paused() is False
    m._note_nse_quote_status(403)                # no helper -> silently ignored
    assert m._waterfall_nse_direct_price("TESTCO") == 5.0
