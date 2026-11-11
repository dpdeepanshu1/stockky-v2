"""E417: a child cancelled from outside comes back from gather(return_exceptions=True) as a CancelledError, which is a BaseException and not an
Exception. /scan/stream turned it into an "invalid" row (no instant fallback, no reason) and the Surprise stream passed it to score_stock as a tick."""
from __future__ import annotations

import asyncio

from test_main_scan_stream_control import (  # noqa: F401
    _AsyncioProxy, _rows, _scripted_gather, _stream as _scan_stream, senv as scan_env, gw as scan_gw,
)
import test_main_surprise_routes as _sr
from test_main_surprise_routes import senv as surprise_env  # noqa: F401


def test_scan_stream_cancelled_child_gets_the_instant_fallback(scan_env, monkeypatch):
    monkeypatch.setattr(scan_gw, "asyncio", _AsyncioProxy(_scripted_gather([asyncio.CancelledError()])))
    scan_env.prices = {"AAA": 7.0}
    scan_env.feeds = {"AAA": {"close": 6.0}}
    scan_env.universe = ["AAA"]
    _, lines = _scan_stream(lite=False)
    row = _rows(lines)[0]
    assert row["fallback_instant"] is True and row["fallback_reason"] == "CancelledError"
    assert scan_env.instant_calls[0][2] == {"price": 7.0}


def test_scan_stream_cancelled_child_with_failing_scorer_is_an_error_row(scan_env, monkeypatch):
    monkeypatch.setattr(scan_gw, "asyncio", _AsyncioProxy(_scripted_gather([asyncio.CancelledError()])))

    def bad(base, fed, tick):
        raise ValueError("nope")

    scan_env.instant = bad
    scan_env.universe = ["AAA"]
    _, lines = _scan_stream(lite=False)
    row = _rows(lines)[0]
    assert row["symbol"] == "AAA" and row["decision"] == "ERROR" and row["error"] == "CancelledError"


def test_surprise_stream_cancelled_quote_is_counted_failed_not_scored(surprise_env):
    eng = surprise_env.engine
    eng.static_cache = {"S00": {}, "S01": {}}
    eng.quotes = {"S00": asyncio.CancelledError(), "S01": {"price": 5}}
    _, lines = _sr._stream()
    assert eng.score_calls == [("S01", {"price": 5})]      # the cancelled tick never reaches score_stock
    done = [ln for ln in lines if ln.get("event") == "done"][0]
    assert done["quotes_ok"] == 1 and done["quotes_failed"] == 1
