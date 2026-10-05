"""group156: ticks read from market-data /live-quote now carry the previous close, so the group155
Tier-3 day-change guard no longer fails open for the (most common) live_quotes path."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from entry_engine import entry
from market_feed import feed as f


def _run(coro):
    return asyncio.run(coro)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://md")


def _handler(ohlc, ltp=100.0):
    updated_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/live-quote/" in request.url.path:
            body = {"source": "angelone", "ltp": ltp, "updated_at": updated_at, "volume": 10}
            if ohlc is not None:
                body["ohlc"] = ohlc
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    return handler


def _tick(ohlc, ltp=100.0):
    async def go():
        async with _client(_handler(ohlc, ltp)) as c:
            return await f.get_quote(c, "ABC")
    return _run(go())


def test_live_quote_tick_carries_prev_close():
    t = _tick({"open": 99, "high": 101, "low": 98, "close": 104.0})
    assert t is not None and t.source.startswith("live_quotes(") and t.prev_close == 104.0


@pytest.mark.parametrize("ohlc", [None, {}, {"close": None}, {"close": 0}, {"close": "x"}, "junk",
                                  {"close": 100.0}])  # close == ltp is the writer's fallback: unknown
def test_missing_or_untrustworthy_close_gives_none(ohlc):
    t = _tick(ohlc)
    assert t is not None and t.prev_close is None


def test_lq_prev_close_never_raises():
    assert f._lq_prev_close(None, 10) is None
    assert f._lq_prev_close({"ohlc": {"close": 9}}, "bad") is None
    assert f._lq_prev_close({"ohlc": {"close": 9}}, 10) == 9.0


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def test_tier3_row_with_live_quotes_tick_down_on_day_is_not_queued(db, monkeypatch):
    """End to end: the exact log case (SAKAR-style, first tick, -5.7% on the day) used to queue at 0.00%."""
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()
    row = models.WatchlistEntry(
        mode="DEMO", symbol="ABC", catalyst_type="volume_shock", catalyst_price=0.0,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="short", decay_half_life_days=1.0,
        entry_band_pct=0.07, source_tier=3, conviction_score=50.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    t = _tick({"close": 106.0}, ltp=100.0)  # -5.66% vs previous close

    async def fake(symbols, **kw):
        return {"ABC": t}
    monkeypatch.setattr(entry, "get_quotes", fake)
    _run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert db.query(models.TradeCandidate).count() == 0
