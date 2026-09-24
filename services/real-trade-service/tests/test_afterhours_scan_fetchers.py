"""
tests/test_afterhours_scan_fetchers.py

SESSION91 round 2 — watchlist_engine/afterhours_scan.py, continuing from
round 1 (tests/test_afterhours_scan_pure_helpers.py, the 5 pure helpers).

Covers the three network-dependent fetch/validate functions, mocked the
same way tests/test_watchlist_sources.py already established for this repo
(patch httpx.AsyncClient + patch the circuit breaker's .call() so no real
HTTP ever fires):

  - _fetch_rss_items      (RSS/Atom fetch — success, XML-parse-error,
                            HTTP-error, connection-error; all degrade to [])
  - _fetch_bulk_deal_hits (api-gateway /stockky-hot bulk_insider_driven
                            bucket — breaker-open, missing symbol, stale-date
                            drop, no-date degrade-open, score clamping,
                            non-numeric score fallback, same-symbol dedup)
  - _validate_symbols     (market_feed.feed.get_preview_quotes — empty
                            input, price filtering, exception degrade-open)

NOT covered here (deferred to round 3 — the DB-writing orchestrators):
  run_afterhours_scan, finalize_nextday_watchlist.

CAVEAT — same as round 1 and sessions 76/77/82c/86: no network in this
sandbox, so these were NOT run through live pytest. Written and hand-traced
against the actual source (including re-reading resilience/circuit_breaker.py
and market_feed/feed.py to get the mocking shape right), not executed.
Run for real before trusting the result:

    cd services/real-trade-service
    python -m pytest tests/test_afterhours_scan_fetchers.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import watchlist_engine.afterhours_scan as ahs


def run(coro):
    return asyncio.run(coro)


def _fake_async_client(get_effect):
    """Build an AsyncMock standing in for `async with httpx.AsyncClient(...) as client`.

    get_effect is one of:
      - a BaseException (class or instance) -> client.get() raises it
      - a list -> client.get() returns successive items (multi-call case)
      - anything else (a single response mock) -> client.get() returns it

    IMPORTANT: a bare MagicMock response object is itself callable, so
    passing it as AsyncMock(side_effect=resp) would make the mock library
    treat it as a side-effect *function* and call it (returning yet another
    auto-mock) instead of returning it — hence the explicit return_value
    path below for the single-response case.
    """
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    is_exc = isinstance(get_effect, BaseException) or (
        isinstance(get_effect, type) and issubclass(get_effect, BaseException)
    )
    if is_exc or isinstance(get_effect, list):
        client.get = AsyncMock(side_effect=get_effect)
    else:
        client.get = AsyncMock(return_value=get_effect)
    return client


def _breaker_calls_fn():
    """Mock CircuitBreaker whose .call() just runs fn() directly (closed-
    breaker behavior) — same helper shape as test_watchlist_sources.py's
    _make_breaker_that_calls_fn."""
    async def _call(fn, *args, fallback=None, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            return await fallback() if fallback else None
    cb = MagicMock()
    cb.call = _call
    return cb


def _breaker_uses_fallback():
    """Mock CircuitBreaker that always skips fn and calls fallback (open-
    breaker behavior)."""
    async def _call(fn, *args, fallback=None, **kwargs):
        return await fallback() if fallback else None
    cb = MagicMock()
    cb.call = _call
    return cb


# ── _fetch_rss_items ─────────────────────────────────────────────────────────

_SAMPLE_RSS_XML = (
    "<rss version=\"2.0\"><channel>"
    "<item><title>Sample headline</title>"
    "<link>https://example.com/1</link>"
    "<pubDate>Wed, 16 Sep 2026 21:32:36 +0530</pubDate></item>"
    "</channel></rss>"
)

_FEED = {"source": "Moneycontrol", "url": "https://example.com/rss", "source_bonus": 10}


class TestFetchRssItems:
    def test_success_returns_parsed_items(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = _SAMPLE_RSS_XML
        resp.status_code = 200
        client = _fake_async_client(resp)

        with patch("httpx.AsyncClient", return_value=client):
            items = run(ahs._fetch_rss_items(_FEED))

        assert len(items) == 1
        assert items[0]["title"] == "Sample headline"
        assert items[0]["link"] == "https://example.com/1"

    def test_non_xml_body_returns_empty_list(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = "<html><body>not a feed</body>"  # malformed / non-XML
        resp.status_code = 200
        client = _fake_async_client(resp)

        with patch("httpx.AsyncClient", return_value=client):
            items = run(ahs._fetch_rss_items(_FEED))

        assert items == []

    def test_http_error_status_returns_empty_list(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 503"))
        client = _fake_async_client(resp)

        with patch("httpx.AsyncClient", return_value=client):
            items = run(ahs._fetch_rss_items(_FEED))

        assert items == []

    def test_connection_error_returns_empty_list(self):
        client = _fake_async_client(ConnectionError("refused"))

        with patch("httpx.AsyncClient", return_value=client):
            items = run(ahs._fetch_rss_items(_FEED))

        assert items == []

    def test_empty_feed_body_returns_empty_list(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.text = "<rss version=\"2.0\"><channel></channel></rss>"
        resp.status_code = 200
        client = _fake_async_client(resp)

        with patch("httpx.AsyncClient", return_value=client):
            items = run(ahs._fetch_rss_items(_FEED))

        assert items == []


# ── _fetch_bulk_deal_hits ────────────────────────────────────────────────────

def _hits_payload(items):
    return {"bulk_insider_driven": items}


class TestFetchBulkDealHits:
    def test_open_breaker_returns_empty_dict(self):
        with patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_uses_fallback()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out == {}

    def test_empty_payload_returns_empty_dict(self):
        client = _fake_async_client(self._json_response({}))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out == {}

    def test_item_missing_symbol_is_skipped(self):
        payload = _hits_payload([{"score": 50, "summary": "no symbol here"}])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out == {}

    def test_no_date_info_degrades_open_and_is_kept(self):
        payload = _hits_payload([{"symbol": "tcs", "score": 72, "summary": "TCS order win"}])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert "TCS" in out  # symbol uppercased
        assert out["TCS"]["score"] == 72.0
        assert out["TCS"]["headline"] == "TCS order win"
        assert out["TCS"]["catalyst_type"] == "bulk_block"

    def test_all_dates_stale_is_dropped(self):
        payload = _hits_payload([{
            "symbol": "OLDCO",
            "score": 60,
            "bulk_deals": [{"published": "2020-01-01"}],
        }])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out == {}

    def test_recent_date_from_insider_transactions_is_kept(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        payload = _hits_payload([{
            "symbol": "FRESHCO",
            "score": 55,
            "insider_transactions": [{"date": today}],
        }])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert "FRESHCO" in out

    def test_non_numeric_score_falls_back_to_catalyst_base_score(self):
        payload = _hits_payload([{"symbol": "NOSCORE", "score": "n/a"}])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out["NOSCORE"]["score"] == ahs._CATALYST_BASE_SCORE["bulk_block"]

    def test_negative_score_clamps_to_zero_and_is_dropped(self):
        payload = _hits_payload([{"symbol": "NEGCO", "score": -20}])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert "NEGCO" not in out

    def test_score_over_100_is_clamped(self):
        payload = _hits_payload([{"symbol": "HOTCO", "score": 500}])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out["HOTCO"]["score"] == 100.0

    def test_duplicate_symbol_keeps_higher_score(self):
        payload = _hits_payload([
            {"symbol": "DUPCO", "score": 40, "summary": "first hit"},
            {"symbol": "DUPCO", "score": 65, "summary": "second, stronger hit"},
        ])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out["DUPCO"]["score"] == 65.0
        assert out["DUPCO"]["headline"] == "second, stronger hit"

    def test_duplicate_symbol_lower_score_second_does_not_overwrite(self):
        payload = _hits_payload([
            {"symbol": "DUPCO2", "score": 65, "summary": "first, stronger hit"},
            {"symbol": "DUPCO2", "score": 40, "summary": "second, weaker hit"},
        ])
        client = _fake_async_client(self._json_response(payload))
        with patch("httpx.AsyncClient", return_value=client), \
             patch("watchlist_engine.afterhours_scan.api_gateway_breaker", _breaker_calls_fn()):
            out = run(ahs._fetch_bulk_deal_hits())
        assert out["DUPCO2"]["score"] == 65.0
        assert out["DUPCO2"]["headline"] == "first, stronger hit"

    @staticmethod
    def _json_response(payload):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=payload)
        return resp


# ── _validate_symbols ─────────────────────────────────────────────────────────

class TestValidateSymbols:
    def test_empty_input_returns_empty_set_no_call(self):
        assert run(ahs._validate_symbols([])) == set()

    def test_filters_to_symbols_with_positive_price(self):
        previews = {
            "TCS": SimpleNamespace(price=3500.0),
            "ZEROCO": SimpleNamespace(price=0.0),
            "NONECO": SimpleNamespace(price=None),
        }
        with patch("market_feed.feed.get_preview_quotes", AsyncMock(return_value=previews)):
            result = run(ahs._validate_symbols(["TCS", "ZEROCO", "NONECO"]))
        assert result == {"TCS"}

    def test_missing_symbol_in_previews_is_excluded(self):
        previews = {"TCS": SimpleNamespace(price=100.0)}
        with patch("market_feed.feed.get_preview_quotes", AsyncMock(return_value=previews)):
            result = run(ahs._validate_symbols(["TCS", "UNKNOWNCO"]))
        assert result == {"TCS"}

    def test_exception_degrades_open_returns_input_as_set(self):
        with patch("market_feed.feed.get_preview_quotes", AsyncMock(side_effect=RuntimeError("boom"))):
            result = run(ahs._validate_symbols(["TCS", "INFY"]))
        assert result == {"TCS", "INFY"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
