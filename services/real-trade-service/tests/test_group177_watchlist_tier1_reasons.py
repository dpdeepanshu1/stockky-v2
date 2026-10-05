"""group 177: Tier 1 fetches Hot Picks + IPO list together, keeps Hot Picks when only the IPO list fails,
and the 'empty/unavailable' line says which case it was."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import watchlist_engine.sources as src  # noqa: E402

_DB = object()
LOGGER = "real-trade-watchlist-sources"


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, payload=None, status_exc=None):
        self._p = payload
        self._e = status_exc

    def json(self):
        return self._p

    def raise_for_status(self):
        if self._e:
            raise self._e


def _client(hot, ipo, delay=0.0):
    """hot / ipo: a _Resp or an Exception to raise."""
    async def get(url, *a, **k):
        if delay:
            await asyncio.sleep(delay)
        item = hot if url.endswith("/stockky-hot") else ipo
        if isinstance(item, BaseException):
            raise item
        return item
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=None)
    c.get = get
    return c


def _breaker(record=None):
    async def _call(fn, fallback=None):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if record is not None:
                record.append(exc)
            return await _fb(fallback)
    cb = MagicMock()
    cb.call = _call
    return cb


async def _fb(fallback):
    if fallback is None:
        return None
    r = fallback()
    return await r if asyncio.iscoroutine(r) else r


def _fetch(client, cached=None, rec=None):
    with patch("httpx.AsyncClient", return_value=client), \
         patch.object(src, "api_gateway_breaker", _breaker(rec)), \
         patch.object(src, "event_service_breaker", _breaker()), \
         patch.object(src, "save_snapshot"), \
         patch.object(src, "load_snapshot", return_value=cached), \
         patch.object(src, "_tier3_volume_shock", new=AsyncMock(return_value=[])):
        return run(src.fetch_watchlist_candidates(_DB, "DEMO"))


HOT = {"bulk_insider_driven": [{"symbol": "TCS", "price": 3000.0, "score": 0.9}], "generated_at": "2026-10-06T10:00:00"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("WATCHLIST_TIER1_IPO_OPTIONAL", raising=False)


def test_ipo_failure_keeps_hot_picks(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = _fetch(_client(_Resp(HOT), httpx.ReadTimeout("")))
    assert [r["symbol"] for r in out] == ["TCS"] and out[0]["source_tier"] == 1
    assert any("/surprise/ipo/list failed (ReadTimeout)" in r.getMessage() for r in caplog.records)


def test_ipo_http_error_keeps_hot_picks():
    err = httpx.HTTPStatusError("502", request=MagicMock(), response=MagicMock())
    out = _fetch(_client(_Resp(HOT), _Resp(status_exc=err)))
    assert [r["symbol"] for r in out] == ["TCS"]


def test_strict_switch_ipo_failure_drops_tier1(monkeypatch):
    monkeypatch.setenv("WATCHLIST_TIER1_IPO_OPTIONAL", "0")
    rec = []
    out = _fetch(_client(_Resp(HOT), httpx.ReadTimeout("")), rec=rec)
    assert out == []                                           # fell through the ladder (Tier 3 stubbed empty)
    assert str(rec[0]).startswith("/surprise/ipo/list ReadTimeout")


def test_hot_failure_error_carries_type_and_route():
    rec = []
    _fetch(_client(httpx.ReadTimeout(""), _Resp([])), rec=rec)
    assert str(rec[0]) == "/stockky-hot ReadTimeout"


def test_hot_http_status_error_carries_type():
    err = httpx.HTTPStatusError("503 Service Unavailable", request=MagicMock(), response=MagicMock())
    rec = []
    _fetch(_client(_Resp(status_exc=err), _Resp([])), rec=rec)
    assert str(rec[0]).startswith("/stockky-hot HTTPStatusError: 503")


def test_failure_logs_a_warning_with_the_failure_reason(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fetch(_client(httpx.ReadTimeout(""), _Resp([])))
    recs = [r for r in caplog.records if "Tier 1 (api-gateway) empty/unavailable" in r.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == logging.WARNING
    assert "call failed or breaker open" in recs[0].getMessage()


def test_empty_answer_is_info_not_warning(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _fetch(_client(_Resp({}), _Resp([])))
    recs = [r for r in caplog.records if "Tier 1 (api-gateway) empty/unavailable" in r.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == logging.INFO
    assert "answered but listed no catalysts" in recs[0].getMessage()


def test_breaker_open_with_empty_cache_names_the_cache(caplog):
    async def _call(fn, fallback=None):
        return await _fb(fallback)
    cb = MagicMock()
    cb.call = _call
    with patch("httpx.AsyncClient", return_value=_client(_Resp({}), _Resp([]))), \
         patch.object(src, "api_gateway_breaker", cb), \
         patch.object(src, "event_service_breaker", _breaker()), \
         patch.object(src, "save_snapshot"), \
         patch.object(src, "load_snapshot", return_value={"hot_picks": {}, "ipo": {}}), \
         patch.object(src, "_tier3_volume_shock", new=AsyncMock(return_value=[])), \
         caplog.at_level(logging.INFO, logger=LOGGER):
        run(src.fetch_watchlist_candidates(_DB, "DEMO"))
    recs = [r for r in caplog.records if "Tier 1 (api-gateway) empty/unavailable" in r.getMessage()]
    assert recs and "cached copy listed no candidates" in recs[0].getMessage()
    assert recs[0].levelno == logging.WARNING


def test_both_routes_are_fetched_at_the_same_time():
    t0 = time.monotonic()
    out = _fetch(_client(_Resp(HOT), _Resp([]), delay=0.3))
    assert [r["symbol"] for r in out] == ["TCS"]
    assert time.monotonic() - t0 < 0.55                        # sequential would be >= 0.6 s


@pytest.mark.parametrize("raw,expected", [("", True), ("1", True), ("0", False), ("false", False), (" OFF ", False)])
def test_ipo_optional_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("WATCHLIST_TIER1_IPO_OPTIONAL", raw)
    assert src._ipo_optional() is expected
