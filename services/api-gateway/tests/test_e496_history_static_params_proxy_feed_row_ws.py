"""E496: a read of `gateway_support/bound_routes_h.py`, `_l.py` and `_m.py` (the batches E494 / E495 had not reached).

1. `/market/history/{symbol}`: a NaN / inf close was kept, so `change_pct` became NaN and the JSON response itself failed (500); a text close raised in
   the change maths and threw away market-data's good bars for the training fallback; a non-dict candle did the same. Bad bars are skipped, a bad
   open / high / low is blanked, and a NaN in the training fallback's reply is turned into null.
2. `/ops/refresh-static-params` called the blocking Redis `delete` (an HTTP call) up to 240 times on the event loop; they run in a worker thread now.
3. `/training/{path}` proxy: a bare NaN in the upstream JSON made `JSONResponse` raise and the caller got "502 Training proxy error".
4. `GET /data-feed/{symbol}` returned the stored row as is: one stored NaN / inf made the response fail (500).
5. WebSocket `subscribe scan:<id>` / `poll_scan`: a stored scan record that is not a dict raised on `.get` and closed the whole socket.

Run from services/api-gateway:  python3 -m pytest tests/test_e496_history_static_params_proxy_feed_row_ws.py -v"""
from __future__ import annotations

import asyncio
import json
import threading
import types

import pytest
from fastapi.testclient import TestClient

from test_main_ops_routes import gw, client, fast_sleep, FakeResp, FakeRedis, _run, _MD_HIST, _candles   # noqa: F401  (client, fast_sleep are fixtures)

NAN, INF = float("nan"), float("inf")


def _strict(obj):
    return json.dumps(obj, allow_nan=False)


# ── 1. market history ────────────────────────────────────────────────────────

def test_a_nan_or_text_close_bar_is_skipped_and_the_chart_stays_valid_json(client):
    bars = _candles(100, 110, 121)
    bars[1]["close"] = NAN
    bars.append({**bars[0], "close": "abc"})
    bars.append("junk")
    client.routes[_MD_HIST()] = FakeResp(200, {"candles": bars})
    out = _run(gw.market_history("TCS", "1y"))
    assert out["source"] == "market-data" and [p["close"] for p in out["points"]] == [100, 121]
    assert out["change_pct"] == 21.0
    _strict(out)


def test_a_bad_open_high_low_volume_is_blanked_not_kept(client):
    bars = _candles(100, 110)
    bars[1].update(open=NAN, high=INF, low="x", volume=NAN)
    client.routes[_MD_HIST()] = FakeResp(200, {"candles": bars})
    out = _run(gw.market_history("TCS", "1y"))
    p = out["points"][1]
    assert p["close"] == 110 and p["open"] is None and p["high"] is None and p["low"] is None and p["volume"] == 0
    _strict(out)


def test_good_bars_are_unchanged(client):
    client.routes[_MD_HIST()] = FakeResp(200, {"candles": _candles(100, 110, 121)})
    out = _run(gw.market_history("tcs.ns", "1y"))
    assert out["points"][0] == {"date": "2026-01-01", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}
    assert out["change_pct"] == 21.0


def test_a_non_dict_market_data_body_goes_to_the_training_fallback(client):
    client.routes[_MD_HIST()] = FakeResp(200, ["junk"])
    client.routes["/api/stock/history/"] = FakeResp(200, {"points": [{"close": 1.0}], "x": NAN})
    out = _run(gw.market_history("TCS", "1y"))
    assert out["source"] == "training" and out["x"] is None
    _strict(out)


# ── 2. refresh-static-params ─────────────────────────────────────────────────

def test_static_param_cache_deletes_run_off_the_event_loop(client, fast_sleep, monkeypatch):
    main_thread = threading.get_ident()
    seen = []

    class _R(FakeRedis):
        def delete(self, key):
            seen.append(threading.get_ident())
            super().delete(key)

    r = _R()

    async def ok(base, cl):
        return {"m": 1}, False

    async def ev(base, cl):
        return {"e": 1}

    monkeypatch.setattr(gw, "_build_scan_universe", lambda: ["AAA"])
    monkeypatch.setattr(gw, "_fetch_fundamental_cached", ok)
    monkeypatch.setattr(gw, "_fetch_events_cached", ev)
    monkeypatch.setattr(gw, "_fetch_news_cached", ev)
    monkeypatch.setattr(gw, "_redis", r)
    out = _run(gw.ops_refresh_static_params())
    assert out["status"] == "ok" and len(r.deleted) == 4           # three per symbol + the hot-stocks key, same order as before
    assert r.deleted[-1] == gw.HOT_STOCKS_CACHE_KEY
    assert seen and all(t != main_thread for t in seen)


# ── 3. training proxy ────────────────────────────────────────────────────────

def test_training_proxy_turns_a_bare_nan_into_null(monkeypatch):
    class _C:
        async def request(self, **kw):
            return types.SimpleNamespace(status_code=200, json=lambda: {"score": NAN, "ok": True}, text="")

    monkeypatch.setattr(gw, "_get_http_client", lambda: _C())
    r = TestClient(gw.app, raise_server_exceptions=False).get("/training/foo/bar")
    assert r.status_code == 200 and r.json() == {"score": None, "ok": True}


# ── 4. /data-feed/{symbol} ───────────────────────────────────────────────────

def test_feed_row_with_nan_is_returned_as_null(monkeypatch):
    monkeypatch.setattr(gw, "_feed_store", lambda: types.SimpleNamespace(get_symbol=lambda s: {"symbol": "TCS", "pe_ratio": NAN, "x": [INF, 1]}))
    out = gw.data_feed_symbol("TCS")
    assert out == {"ok": True, "data": {"symbol": "TCS", "pe_ratio": None, "x": [None, 1]}}
    r = TestClient(gw.app, raise_server_exceptions=False).get("/data-feed/TCS")
    assert r.status_code == 200 and r.json()["data"]["pe_ratio"] is None


def test_good_feed_row_is_unchanged(monkeypatch):
    row = {"symbol": "TCS", "pe_ratio": 25.5}
    monkeypatch.setattr(gw, "_feed_store", lambda: types.SimpleNamespace(get_symbol=lambda s: row))
    assert gw.data_feed_symbol("TCS") == {"ok": True, "data": row}


# ── 5. websocket scan record ─────────────────────────────────────────────────

class _FakeWS:
    """Scripted socket: hands out the queued client frames, then reports a disconnect (so nothing can hang)."""
    headers = {}

    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def accept(self):
        pass

    async def receive_text(self):
        if not self.frames:
            raise gw.WebSocketDisconnect()
        return json.dumps(self.frames.pop(0))

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        pass


@pytest.mark.parametrize("stored", ["junk", ["a"], 7])
def test_a_non_dict_scan_record_does_not_close_the_socket(monkeypatch, stored):
    monkeypatch.setattr(gw, "_ensure_quote_loop", lambda: None)
    monkeypatch.setattr(gw, "_ensure_jobs_loop", lambda: None)
    monkeypatch.setattr(gw, "_redis_get", lambda k: stored)
    ws = _FakeWS([{"action": "subscribe", "channel": "scan:t1"}, {"action": "poll_scan", "task_id": "t1"}, {"action": "ping"}])
    asyncio.run(asyncio.wait_for(gw.websocket_endpoint(ws), timeout=10))
    assert [m["type"] for m in ws.sent] == ["connected", "subscribed", "scan_status", "scan_status", "pong"]   # still connected after both
