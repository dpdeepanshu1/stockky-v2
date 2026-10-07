"""
group233 (api-gateway): bulk quotes instead of one call per symbol, closed-market handling.

* `_fetch_prices_bulk_async(..., bulk_min=1, per_symbol_fallback=False)` prices even a handful of symbols with one
  POST /quotes/bulk and never sends GET /quote/{sym} (hot-picks price pass).
* With the market closed, market-data answers from the last close (hours-old fetched_at): the helpers accept those
  rows, reuse them for a while, and skip the per-symbol leftover.

Run from services/api-gateway:
    python3 -m pytest tests/test_group233_closed_market_and_bulk_only.py -v
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def run(coro):
    return asyncio.run(coro)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _iso(age_s=0.0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()


class FakeClient:
    def __init__(self, bulk_prices=None, single=None, bulk_age=0.0):
        self.gets, self.posts = [], []
        self.bulk_prices, self.single, self.bulk_age = bulk_prices or {}, single or {}, bulk_age

    async def get(self, url, timeout=None, **k):
        sym = url.rsplit("/", 1)[1]
        self.gets.append(sym)
        px = self.single.get(sym)
        return Resp(200, {"price": px}) if px else Resp(404, {})

    async def post(self, url, json=None, timeout=None, **k):
        self.posts.append(list(json["symbols"]))
        rows = [{"symbol": s, "price": self.bulk_prices[s], "fetched_at": _iso(self.bulk_age)}
                for s in json["symbols"] if s in self.bulk_prices]
        return Resp(200, {"quotes": rows})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gw._GW_BULK_PX_CACHE.clear()
    for k in ("GATEWAY_BULK_QUOTE_MIN", "GATEWAY_BULK_QUOTE_CACHE_S", "GATEWAY_BULK_QUOTE_MAX_AGE_S",
              "GATEWAY_BULK_QUOTE_CLOSED_MAX_AGE_S", "GATEWAY_BULK_QUOTE_CLOSED_CACHE_S",
              "GATEWAY_BULK_QUOTE_CLOSED_AWARE"):
        monkeypatch.delenv(k, raising=False)
    yield
    gw._GW_BULK_PX_CACHE.clear()


class TestBulkOnly:
    def test_few_symbols_still_go_through_bulk_when_asked(self):
        c = FakeClient(bulk_prices={"A": 1.0, "B": 2.0, "C": 3.0})
        out = run(gw._fetch_prices_bulk_async(["A", "B", "C"], c, bulk_min=1, per_symbol_fallback=False))
        assert out == {"A": 1.0, "B": 2.0, "C": 3.0}
        assert c.posts == [["A", "B", "C"]] and c.gets == []

    def test_unpriced_symbols_are_left_alone_not_sent_per_symbol(self):
        c = FakeClient(bulk_prices={"A": 1.0}, single={"B": 2.0})
        out = run(gw._fetch_prices_bulk_async(["A", "B"], c, bulk_min=1, per_symbol_fallback=False))
        assert out == {"A": 1.0} and c.gets == []

    def test_defaults_are_unchanged(self):
        c = FakeClient(single={"A": 1.0, "B": 2.0})
        out = run(gw._fetch_prices_bulk_async(["A", "B"], c))     # < 15 symbols: old per-symbol path
        assert out == {"A": 1.0, "B": 2.0} and c.posts == [] and sorted(c.gets) == ["A", "B"]


class TestClosedMarket:
    @pytest.fixture(autouse=True)
    def _closed(self, monkeypatch):
        monkeypatch.setattr(gw, "_gw_quotes_closed", lambda: True)

    def test_hours_old_last_close_rows_are_accepted(self):
        c = FakeClient(bulk_prices={"A": 10.0}, bulk_age=5 * 3600)
        out = run(gw._fetch_prices_bulk_first(["A"], c))
        assert out == {"A": 10.0}

    def test_open_market_still_rejects_old_rows(self, monkeypatch):
        monkeypatch.setattr(gw, "_gw_quotes_closed", lambda: False)
        c = FakeClient(bulk_prices={"A": 10.0}, bulk_age=5 * 3600)
        assert run(gw._fetch_prices_bulk_first(["A"], c)) == {}

    def test_closed_prices_are_reused_for_ten_minutes(self):
        c = FakeClient(bulk_prices={"A": 10.0}, bulk_age=3600)
        run(gw._fetch_prices_bulk_first(["A"], c))
        gw._GW_BULK_PX_CACHE["A"] = (gw.time.monotonic() - 120, 10.0)   # 2 minutes old
        c.posts.clear()
        assert run(gw._fetch_prices_bulk_first(["A"], c)) == {"A": 10.0} and c.posts == []

    def test_per_symbol_leftover_is_skipped_after_a_bulk_pass(self):
        syms = [f"S{i}" for i in range(20)]
        c = FakeClient(bulk_prices={s: 1.0 for s in syms[:15]}, single={"S19": 9.0}, bulk_age=3600)
        out = run(gw._fetch_prices_bulk_async(syms, c))
        assert len(out) == 15 and c.gets == []

    def test_off_switch(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setenv("GATEWAY_BULK_QUOTE_CLOSED_AWARE", "0")
        assert gw._gw_quotes_closed() is False

    def test_phase_mapping(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.delenv("GATEWAY_BULK_QUOTE_CLOSED_AWARE", raising=False)
        for phase, expected in (("closed", True), ("holiday", True), ("open", False),
                                ("preopen", False), ("post", False)):
            monkeypatch.setattr(gw, "_market_session_phase_ist", lambda p=phase: p)
            assert gw._gw_quotes_closed() is expected, phase
