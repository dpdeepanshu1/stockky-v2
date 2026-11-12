"""E478: two small Surprise-path defects.
 1. `_get_52w_extreme_symbols` read the change with `item.get("change_pct") or item.get("pChange") or ...`, so a real 0.0 fell through to a missing
    key (None) and the flat symbol was ADDED although the rule is |change| >= 3%. `_first_present` keeps a real 0 (a missing / None change is still
    added, as before).
 2. `_surprise_closed_market_result` did `int(limit)` inside its catch-all, so a junk `limit` abandoned the saved result and returned None, which
    starts a live sweep on a closed market. A junk limit now serves the saved list whole.
Needs fastapi (imports main). Runs with pytest."""
from __future__ import annotations

import asyncio
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import main as gw  # noqa: E402


def test_first_present_keeps_a_real_zero():
    f = gw._first_present
    assert f({"a": 0.0, "b": 5}, ("a", "b")) == 0.0
    assert f({"a": None, "b": 5}, ("a", "b")) == 5
    assert f({"b": 0}, ("a", "b")) == 0
    assert f({}, ("a", "b")) is None


def _setup(monkeypatch, gainers, nse_rows=None):
    monkeypatch.setattr(gw, "market_top_gainers", lambda: {"data": gainers})
    monkeypatch.setattr(gw, "market_most_active", lambda: {"data": []})
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: {"data": nse_rows or []})
    monkeypatch.setattr(gw, "_clean_equity_symbol", lambda s: (s or "").upper() or None)


def test_a_flat_symbol_is_not_added_by_the_three_percent_rule(monkeypatch):
    _setup(monkeypatch, [
        {"symbol": "flat", "change_pct": 0.0}, {"symbol": "flat2", "pChange": 0},
        {"symbol": "up", "change_pct": 3.5}, {"symbol": "nochg"},
        {"symbol": "zero_first", "change_pct": 0.0, "pChange": 9.0},   # the first present key decides
    ])
    out = gw._get_52w_extreme_symbols()
    assert out == ["UP", "NOCHG"]


def test_nse_board_zero_change_is_not_added(monkeypatch):
    _setup(monkeypatch, [], [{"symbol": "nflat", "pChange": 0.0}, {"symbol": "nup", "pChange": 4}, {"symbol": "nnone"}])
    assert gw._get_52w_extreme_symbols() == ["NUP", "NNONE"]


class _Engine:
    def __init__(self):
        self._last_result = {"stocks": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]}
        self._last_scan_ts = time.time()


def test_junk_limit_serves_the_saved_list_whole(monkeypatch):
    monkeypatch.setattr(gw, "_surprise_closed_serve_enabled", lambda: True)
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    for junk in ("abc", [1], {}, float("nan")):
        out = asyncio.run(gw._surprise_closed_market_result(_Engine(), junk))
        assert out is not None and out["market_closed"] is True and len(out["stocks"]) == 3, junk
    out = asyncio.run(gw._surprise_closed_market_result(_Engine(), 2))
    assert [s["symbol"] for s in out["stocks"]] == ["A", "B"]
