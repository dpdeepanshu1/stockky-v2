"""tests/test_main_feed_write_routes.py — coverage for api-gateway/main.py, slice 17 (lines 10046-10337)

Pass 75. The feed read/write routes and the two price helpers that follow the audit block:

* `GET /data-feed/{symbol}` (+ 2 aliases) — hit / miss envelope;
* `POST /data-feed/update` (+ alias) — body validation, nested `fundamental`/`events` vs flat payload,
  the over-cap REJECTED envelope, the 300-char error envelope;
* `POST /data-feed/batch` (+ alias) — `feeds` / `items` / `data` / bare symbol->payload shapes, over-cap
  skipping, concurrent writes with per-symbol failure isolation;
* `POST /data-feed/update-batch` (+ alias) — symbol cleaning + DATA_FEED_UPDATE_BATCH_MAX cap, the
  fundamental/events fetch (429/503 -> rate-limit monitor), over-cap / empty / exception result rows,
  0.15s pacing;
* `_safe_float` and `_feed_resolved_price` (every fallback tier).

Everything downstream is faked: the feed store, `data_feed.save_stock_feed` / `extract_feed_payload`, the
shared async httpx client, `rate_limit_monitor.record`, `asyncio.sleep`. Nothing touches the network or a
database. Findings are pinned as current behaviour and marked ``NOT FIXED``; the ones fixed afterwards
(over-cap pacing sleep, infinity in `_safe_float`, flat-zero vs metrics price) now pin the fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_feed_write_routes.py -v
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import data_feed
import price_resolver
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, body=None, raises=False):
        self._body, self._raises = body, raises

    async def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._body


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.puts = []
        self.put_raises = set()
        self.get_raises = False

    def get_symbol(self, sym):
        if self.get_raises:
            raise RuntimeError("store down")
        return self.rows.get(sym)

    def put_symbol(self, sym, row, ttl=None):
        if sym in self.put_raises:
            raise RuntimeError("put failed " + sym)
        self.puts.append((sym, row, ttl))


@pytest.fixture
def store(monkeypatch):
    s = FakeStore()
    monkeypatch.setattr(gw, "_feed_store", lambda: s)
    return s


@pytest.fixture
def tc():
    return TestClient(gw.app, raise_server_exceptions=False)


@pytest.fixture
def saver(monkeypatch):
    """Fakes data_feed.save_stock_feed + extract_feed_payload (both imported lazily inside the routes)."""
    s = types.SimpleNamespace(saved=[], raises=set(), generic_raise=False, extract_calls=[],
                              extract_raises=False)

    def save(base, row, ttl=None):
        if s.generic_raise or base in s.raises:
            raise RuntimeError("E" * 400 if s.generic_raise else f"save failed {base}")
        s.saved.append((base, row))

    def extract(symbol, fundamental=None, events=None, extra=None):
        s.extract_calls.append((symbol, fundamental, events, extra))
        if s.extract_raises:
            raise RuntimeError("extract boom")
        row = {"symbol": symbol, "extracted": True}
        if isinstance(fundamental, dict):
            row.update(fundamental)
        if isinstance(extra, dict):
            row.update(extra)
        return row

    monkeypatch.setattr(data_feed, "save_stock_feed", save)
    monkeypatch.setattr(data_feed, "extract_feed_payload", extract)
    return s


@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)


# ══ GET /data-feed/{symbol} ══════════════════════════════════════════════════

class TestFeedSymbol:
    def test_hit(self, store):
        store.rows["TCS"] = {"price": 1}
        assert gw.data_feed_symbol("TCS") == {"ok": True, "data": {"price": 1}}

    def test_miss_uppercases_the_symbol(self, store):
        assert gw.data_feed_symbol("tcs") == {"ok": False, "symbol": "TCS", "detail": "No data feed entry"}

    def test_empty_row_counts_as_a_miss(self, store):
        store.rows["TCS"] = {}
        assert gw.data_feed_symbol("TCS")["ok"] is False

    def test_routed_on_all_three_paths(self, store, tc):
        store.rows["TCS"] = {"price": 1}
        for path in ("/data-feed/TCS", "/api/data-feed/TCS", "/api/feed/TCS"):
            assert tc.get(path).json() == {"ok": True, "data": {"price": 1}}


# ══ POST /data-feed/update ═══════════════════════════════════════════════════

class TestUpdateSingle:
    def test_bad_json_is_treated_as_empty_and_needs_a_symbol(self, saver):
        out = _run(gw.data_feed_update_single(FakeRequest(raises=True)))
        assert out == {"ok": False, "error": "symbol required"}

    @pytest.mark.parametrize("body", [[1, 2], "str", 5, None])
    def test_non_object_json_is_rejected(self, saver, body):
        out = _run(gw.data_feed_update_single(FakeRequest(body)))
        assert out == {"ok": False, "error": "JSON object required"}

    @pytest.mark.parametrize("sym", [None, "", 0])
    def test_missing_or_falsy_symbol(self, saver, sym):
        assert _run(gw.data_feed_update_single(FakeRequest({"symbol": sym})))["error"] == "symbol required"

    def test_flat_payload_is_saved_under_the_cleaned_symbol(self, saver):
        out = _run(gw.data_feed_update_single(FakeRequest({"symbol": " tcs.ns ", "rsi": 55})))
        assert out == {"ok": True, "status": "SUCCESS", "symbol": "TCS"}
        assert saver.saved == [("TCS", {"rsi": 55, "symbol": "TCS"})]
        assert saver.extract_calls == []

    def test_bo_suffix_is_stripped(self, saver):
        assert _run(gw.data_feed_update_single(FakeRequest({"symbol": "infy.bo"})))["symbol"] == "INFY"

    def test_nested_fundamental_and_events_go_through_extract(self, saver):
        body = {"symbol": "TCS", "fundamental": {"pe": 20}, "events": {"e": 1}, "note": "x"}
        out = _run(gw.data_feed_update_single(FakeRequest(body)))
        assert out["ok"] is True
        sym, fund, events, extra = saver.extract_calls[0]
        assert sym == "TCS" and fund == {"pe": 20} and events == {"e": 1} and extra == {"note": "x"}
        assert saver.saved[0][1]["extracted"] is True

    def test_events_only_uses_the_whole_body_as_fundamental(self, saver):
        body = {"symbol": "TCS", "events": {"e": 1}, "pe": 9}
        _run(gw.data_feed_update_single(FakeRequest(body)))
        _, fund, events, extra = saver.extract_calls[0]
        assert fund == {"events": {"e": 1}, "pe": 9} and events == {"e": 1} and extra == {"pe": 9}

    def test_non_dict_fundamental_falls_back_to_body_and_events_to_none(self, saver):
        body = {"symbol": "TCS", "fundamental": "oops", "events": ["x"]}
        _run(gw.data_feed_update_single(FakeRequest(body)))
        _, fund, events, extra = saver.extract_calls[0]
        assert fund == {"fundamental": "oops", "events": ["x"]} and events is None and extra == {}

    def test_empty_nested_blocks_fall_through_to_the_flat_path(self, saver):
        _run(gw.data_feed_update_single(FakeRequest({"symbol": "TCS", "fundamental": {}, "events": {}})))
        assert saver.extract_calls == []
        assert saver.saved[0][1] == {"fundamental": {}, "events": {}, "symbol": "TCS"}

    def test_over_cap_price_is_rejected_and_not_saved(self, saver, cap):
        out = _run(gw.data_feed_update_single(FakeRequest({"symbol": "pricey", "price": 9000})))
        assert out == {"ok": False, "status": "REJECTED", "symbol": "PRICEY",
                       "error": "price above ₹5000 cap — not saved"}
        assert saver.saved == []

    def test_known_expensive_symbol_is_rejected_by_name(self, saver, cap):
        out = _run(gw.data_feed_update_single(FakeRequest({"symbol": "MRF"})))
        assert out["status"] == "REJECTED" and saver.saved == []

    def test_under_cap_price_is_saved(self, saver, cap):
        assert _run(gw.data_feed_update_single(FakeRequest({"symbol": "OK", "price": 100})))["ok"] is True

    def test_no_cap_means_any_price_is_saved(self, saver, monkeypatch):
        monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        assert _run(gw.data_feed_update_single(FakeRequest({"symbol": "MRF", "price": 99999})))["ok"] is True

    def test_save_failure_is_an_error_envelope_truncated_to_300(self, saver):
        saver.generic_raise = True
        out = _run(gw.data_feed_update_single(FakeRequest({"symbol": "TCS"})))
        assert out == {"ok": False, "error": "E" * 300}

    def test_extract_failure_is_an_error_envelope(self, saver):
        saver.extract_raises = True
        out = _run(gw.data_feed_update_single(FakeRequest({"symbol": "TCS", "fundamental": {"a": 1}})))
        assert out == {"ok": False, "error": "extract boom"}

    def test_routed_on_both_paths(self, saver, tc):
        for path in ("/api/feed/update", "/data-feed/update"):
            r = tc.post(path, json={"symbol": "TCS", "rsi": 1})
            assert r.status_code == 200 and r.json()["status"] == "SUCCESS"
        assert len(saver.saved) == 2

    def test_unparseable_http_body_is_symbol_required(self, saver, tc):
        r = tc.post("/data-feed/update", content=b"not json", headers={"content-type": "application/json"})
        assert r.json() == {"ok": False, "error": "symbol required"}


# ══ POST /data-feed/batch ════════════════════════════════════════════════════

class TestUpdateBatch:
    def test_bad_json_and_non_dict_payloads_provide_no_feeds(self, saver):
        for req in (FakeRequest(raises=True), FakeRequest([1]), FakeRequest(None), FakeRequest({})):
            assert _run(gw.data_feed_update_batch(req)) == {"ok": False, "error": "No feeds provided", "count": 0}

    def test_feeds_dict(self, saver):
        body = {"feeds": {"tcs.ns": {"rsi": 1}, "INFY.BO": {"rsi": 2}}}
        out = _run(gw.data_feed_update_batch(FakeRequest(body)))
        assert out == {"ok": True, "status": "SUCCESS", "count": 2, "rejected_over_cap": 0}
        assert sorted(saver.saved) == [("INFY", {"rsi": 2, "symbol": "INFY"}), ("TCS", {"rsi": 1, "symbol": "TCS"})]

    @pytest.mark.parametrize("key", ["items", "data"])
    def test_items_and_data_keys_are_accepted(self, saver, key):
        out = _run(gw.data_feed_update_batch(FakeRequest({key: {"TCS": {"a": 1}}})))
        assert out["count"] == 1 and [b for b, _ in saver.saved] == ["TCS"]    # not the wrapper key "ITEMS"/"DATA"

    def test_feeds_key_wins_over_items(self, saver):
        _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {}}, "items": {"BBB": {}}})))
        assert [b for b, _ in saver.saved] == ["AAA"]

    def test_list_form_keeps_only_dicts_with_a_symbol(self, saver):
        body = {"feeds": [{"symbol": "AAA", "x": 1}, {"x": 2}, "str", {"symbol": ""}, {"symbol": "BBB"}]}
        out = _run(gw.data_feed_update_batch(FakeRequest(body)))
        assert out["count"] == 2 and sorted(b for b, _ in saver.saved) == ["AAA", "BBB"]

    def test_bare_symbol_to_payload_mapping(self, saver):
        out = _run(gw.data_feed_update_batch(FakeRequest({"AAA": {"x": 1}, "BBB": {"x": 2}})))
        assert out["count"] == 2

    def test_bare_mapping_with_a_non_dict_value_is_not_a_feed(self, saver):
        out = _run(gw.data_feed_update_batch(FakeRequest({"AAA": {"x": 1}, "note": "hi"})))
        assert out == {"ok": False, "error": "No feeds provided", "count": 0}

    def test_non_dict_non_list_feeds_value_falls_to_bare_mapping_rules(self, saver):
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": "oops"})))
        assert out["ok"] is False and out["count"] == 0

    def test_non_dict_rows_are_skipped(self, saver):
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {"x": 1}, "BBB": "no", "CCC": None}})))
        assert out["count"] == 1 and saver.saved[0][0] == "AAA"

    def test_symbol_in_row_is_overwritten_by_the_key(self, saver):
        _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {"symbol": "ZZZ"}}})))
        assert saver.saved[0][1]["symbol"] == "AAA"

    def test_over_cap_rows_are_counted_and_skipped(self, saver, cap):
        feeds = {"OK": {"price": 10}, "BIG": {"price": 9000}, "MRF": {}}
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": feeds})))
        assert out == {"ok": True, "status": "SUCCESS", "count": 1, "rejected_over_cap": 2}
        assert [b for b, _ in saver.saved] == ["OK"]

    def test_all_rejected_still_succeeds_with_zero_count(self, saver, cap):
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"BIG": {"price": 9000}}})))
        assert out["count"] == 0 and out["rejected_over_cap"] == 1 and saver.saved == []

    def test_one_failed_write_does_not_stop_the_others(self, saver):
        saver.raises.add("BBB")
        feeds = {"AAA": {}, "BBB": {}, "CCC": {}}
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": feeds})))
        assert out["count"] == 2 and sorted(b for b, _ in saver.saved) == ["AAA", "CCC"]

    def test_writes_run_off_the_event_loop_thread(self, saver, monkeypatch):
        import threading
        seen = []

        def save(base, row, ttl=None):
            seen.append(threading.current_thread() is threading.main_thread())

        monkeypatch.setattr(data_feed, "save_stock_feed", save)

        async def go():
            return await gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {}}}))

        assert _run(go())["count"] == 1 and seen == [False]

    def test_unexpected_failure_is_an_error_envelope_truncated_to_300(self, saver, monkeypatch):
        def boom(row, symbol=None):
            raise RuntimeError("E" * 400)

        monkeypatch.setattr(gw, "_row_price_over_cap", boom)
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {}}})))
        assert out == {"ok": False, "error": "E" * 300, "count": 0}

    def test_import_failure_is_an_error_envelope(self, saver, monkeypatch):
        monkeypatch.delattr(data_feed, "save_stock_feed")
        out = _run(gw.data_feed_update_batch(FakeRequest({"feeds": {"AAA": {}}})))
        assert out["ok"] is False and out["count"] == 0 and "save_stock_feed" in out["error"]

    def test_routed_on_both_paths(self, saver, tc):
        for path in ("/api/feed/batch", "/data-feed/batch"):
            assert tc.post(path, json={"feeds": {"AAA": {"x": 1}}}).json()["count"] == 1


# ══ POST /data-feed/update-batch ═════════════════════════════════════════════

class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code, self._data, self._raises = status, data, json_raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._data


class FakeClient:
    def __init__(self):
        self.routes = {}
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        for frag, out in self.routes.items():
            if frag in url:
                if isinstance(out, Exception):
                    raise out
                return out
        return FakeResp(404)


@pytest.fixture
def ub(store, saver, monkeypatch):
    u = types.SimpleNamespace(client=FakeClient(), sleeps=[], monitor=[], monitor_raises=False)

    async def fake_sleep(d):
        u.sleeps.append(d)

    def record(**kw):
        if u.monitor_raises:
            raise RuntimeError("monitor down")
        u.monitor.append(kw)

    monkeypatch.setattr(gw, "_get_http_client", lambda: u.client)
    monkeypatch.setattr(gw, "FUNDAMENTAL_URL", "http://fund.test")
    monkeypatch.setattr(gw, "EVENT_URL", "http://event.test")
    monkeypatch.setattr(gw.rate_limit_monitor, "record", record)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.delenv("DATA_FEED_UPDATE_BATCH_MAX", raising=False)
    return u


class TestUpdateBatchRefresh:
    @pytest.mark.parametrize("body", [None, [], "x", {}, {"symbols": []}, {"symbols": "TCS"}, {"symbols": None}])
    def test_symbols_list_required(self, ub, body):
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest(body)))
        assert out == {"ok": False, "error": "symbols list required", "ok_count": 0, "error_count": 0}

    def test_bad_json_needs_symbols(self, ub):
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest(raises=True)))
        assert out["error"] == "symbols list required"

    def test_all_blank_symbols_is_no_valid_symbols(self, ub):
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["", None, 0]})))
        assert out == {"ok": False, "error": "no valid symbols", "ok_count": 0, "error_count": 0}

    def test_happy_path_fetches_both_upstreams_and_stores(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(200, {"metrics": {"pe": 1}})
        ub.client.routes["/events/TCS"] = FakeResp(200, {"e": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": [" tcs.ns "]})))
        assert out == {"ok": True, "ok_count": 1, "error_count": 0, "processed": 1,
                       "results": [{"symbol": "TCS", "ok": True}]}
        assert ub.client.calls == [("http://fund.test/analyze/TCS", 35), ("http://event.test/events/TCS", 20)]
        sym, row, ttl = store.puts[0]
        assert sym == "TCS" and row["extracted"] is True and ttl == gw.DATA_FEED_TTL
        assert ub.sleeps == [0.15]

    def test_real_extract_feed_payload_shape_is_stored(self, ub, store, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr(gw, "_feed_store", lambda: store)
        monkeypatch.setattr(gw, "_get_http_client", lambda: ub.client)
        monkeypatch.setattr(gw, "FUNDAMENTAL_URL", "http://fund.test")
        monkeypatch.setattr(gw, "EVENT_URL", "http://event.test")

        async def fake_sleep(d):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        ub.client.routes["/analyze/TCS"] = FakeResp(200, {"metrics": {"pe_ratio": 22}})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["ok_count"] == 1 and store.puts[0][1]["symbol"] == "TCS"

    def test_fundamental_only_is_enough(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(200, {"x": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["ok_count"] == 1 and store.puts

    def test_events_only_is_enough(self, ub, store):
        ub.client.routes["/events/TCS"] = FakeResp(200, {"e": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["ok_count"] == 1

    def test_neither_upstream_is_a_no_fund_events_error(self, ub, store):
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["error_count"] == 1 and out["ok_count"] == 0
        assert out["results"] == [{"symbol": "TCS", "ok": False, "error": "no fund/events"}]
        assert store.puts == [] and ub.sleeps == [0.15]

    def test_empty_json_bodies_count_as_nothing(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(200, {})
        ub.client.routes["/events/TCS"] = FakeResp(200, None)
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["results"][0]["error"] == "no fund/events"

    @pytest.mark.parametrize("status", [429, 503])
    def test_rate_limited_fundamental_is_reported_to_the_monitor(self, ub, store, status):
        ub.client.routes["/analyze/TCS"] = FakeResp(status)
        ub.client.routes["/events/TCS"] = FakeResp(200, {"e": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert ub.monitor == [{"source": "analysis", "status": status, "path": "/fundamental/TCS",
                               "detail": "update-batch", "symbol": "TCS"}]
        assert out["ok_count"] == 1                        # events alone still wrote the row

    def test_other_error_statuses_are_not_reported(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(500)
        _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert ub.monitor == []

    def test_rate_limited_events_are_not_reported(self, ub, store):
        ub.client.routes["/events/TCS"] = FakeResp(429)
        _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert ub.monitor == []

    def test_monitor_failure_is_swallowed(self, ub, store):
        ub.monitor_raises = True
        ub.client.routes["/analyze/TCS"] = FakeResp(429)
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["error_count"] == 1

    def test_fundamental_exception_is_swallowed_and_events_still_tried(self, ub, store):
        ub.client.routes["/analyze/TCS"] = RuntimeError("fund down")
        ub.client.routes["/events/TCS"] = FakeResp(200, {"e": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["ok_count"] == 1

    def test_events_exception_is_swallowed_and_fundamental_still_used(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(200, {"x": 1})
        ub.client.routes["/events/TCS"] = RuntimeError("events down")
        assert _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))["ok_count"] == 1

    def test_bad_json_from_upstream_is_swallowed(self, ub, store):
        ub.client.routes["/analyze/TCS"] = FakeResp(200, json_raises=True)
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["TCS"]})))
        assert out["error_count"] == 1 and out["results"][0]["error"] == "no fund/events"

    def test_over_cap_row_is_an_error_row_and_still_paces(self, ub, store, cap):
        """FIXED: the over-cap `continue` used to skip the 0.15s pacing sleep, so a run of over-cap symbols
        hammered the upstream at full speed. Every outcome (incl. over-cap) now sleeps once per symbol."""
        ub.client.routes["/analyze/BIG"] = FakeResp(200, {"price": 9000})
        ub.client.routes["/analyze/OK"] = FakeResp(200, {"price": 10})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["BIG", "OK"]})))
        assert out["results"] == [{"symbol": "BIG", "ok": False, "error": "price above cap"},
                                  {"symbol": "OK", "ok": True}]
        assert out["ok_count"] == 1 and out["error_count"] == 1 and [p[0] for p in store.puts] == ["OK"]
        assert ub.sleeps == [0.15, 0.15]                   # one pacing sleep per symbol, over-cap included

    def test_a_run_of_over_cap_symbols_is_paced(self, ub, store, cap):
        for sym in ("B1", "B2", "B3"):
            ub.client.routes["/analyze/" + sym] = FakeResp(200, {"price": 9000})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["B1", "B2", "B3"]})))
        assert out["ok_count"] == 0 and out["error_count"] == 3 and store.puts == []
        assert ub.sleeps == [0.15, 0.15, 0.15]

    def test_store_write_failure_is_isolated_to_that_symbol(self, ub, store):
        store.put_raises.add("AAA")
        ub.client.routes["/analyze/"] = FakeResp(200, {"x": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["AAA", "BBB"]})))
        assert out["results"][0] == {"symbol": "AAA", "ok": False, "error": "put failed AAA"}
        assert out["results"][1] == {"symbol": "BBB", "ok": True}
        assert out["ok_count"] == 1 and out["error_count"] == 1 and ub.sleeps == [0.15, 0.15]

    def test_write_error_message_is_truncated_to_120(self, ub, store, saver):
        saver.extract_raises = True
        ub.client.routes["/analyze/AAA"] = FakeResp(200, {"x": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["AAA"]})))
        assert out["results"][0]["error"] == "extract boom"

    def test_long_error_is_cut_to_120(self, ub, store):
        store.put_raises.add("X" * 200)
        ub.client.routes["/analyze/"] = FakeResp(200, {"x": 1})
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["X" * 200]})))
        assert len(out["results"][0]["error"]) == 120

    def test_default_cap_is_15_symbols(self, ub, store):
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": [f"S{i}" for i in range(20)]})))
        assert out["processed"] == 15 and len(out["results"]) == 15 and out["results"][-1]["symbol"] == "S14"

    def test_cap_is_env_configurable(self, ub, store, monkeypatch):
        monkeypatch.setenv("DATA_FEED_UPDATE_BATCH_MAX", "2")
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["A", "B", "C"]})))
        assert out["processed"] == 2

    def test_blank_entries_are_dropped_before_the_cap(self, ub, store, monkeypatch):
        monkeypatch.setenv("DATA_FEED_UPDATE_BATCH_MAX", "2")
        out = _run(gw.data_feed_update_batch_refresh(FakeRequest({"symbols": ["", "A", None, "B", "C"]})))
        assert [r["symbol"] for r in out["results"]] == ["A", "B"]

    def test_routed_on_both_paths(self, ub, store, tc):
        for path in ("/api/feed/update-batch", "/data-feed/update-batch"):
            r = tc.post(path, json={"symbols": ["TCS"]})
            assert r.status_code == 200 and r.json()["processed"] == 1


# ══ _safe_float ══════════════════════════════════════════════════════════════

class TestSafeFloat:
    @pytest.mark.parametrize("val", [None, ""])
    def test_none_and_empty_return_default(self, val):
        assert gw._safe_float(val) == 0.0
        assert gw._safe_float(val, default=-1.0) == -1.0

    @pytest.mark.parametrize("val,exp", [(5, 5.0), (2.5, 2.5), (0, 0.0), (-3, -3.0), (True, 1.0)])
    def test_numbers(self, val, exp):
        assert gw._safe_float(val) == exp

    def test_nan_number_returns_default(self):
        assert gw._safe_float(float("nan"), default=7.0) == 7.0

    def test_nan_string_returns_default(self):
        assert gw._safe_float("nan", default=7.0) == 7.0

    @pytest.mark.parametrize("val", ["inf", "-inf", "Infinity", "-Infinity", "1e999", float("inf"), float("-inf")])
    def test_infinity_returns_default(self, val):
        # FIXED: only NaN used to be guarded, so "inf" / float('inf') leaked through as a real infinity
        # (and then poisoned any comparison / sum downstream). Infinities now fall back to the default too.
        assert gw._safe_float(val, default=7.0) == 7.0
        assert gw._safe_float(val) == 0.0

    def test_huge_int_that_overflows_float_returns_default(self):
        assert gw._safe_float(10 ** 400, default=4.0) == 4.0

    @pytest.mark.parametrize("val,exp", [("1,234.5", 1234.5), (" 1 2 ", 12.0), ("  7  ", 7.0), ("-4", -4.0),
                                         ("1,20,000", 120000.0)])
    def test_strings_with_commas_and_spaces(self, val, exp):
        assert gw._safe_float(val) == exp

    @pytest.mark.parametrize("val", ["-", "NA", "n/a", "None", "null", "NULL", "  ", ","])
    def test_sentinel_strings_return_default(self, val):
        assert gw._safe_float(val, default=9.0) == 9.0

    @pytest.mark.parametrize("val", ["abc", [1], {"a": 1}, object()])
    def test_garbage_returns_default(self, val):
        assert gw._safe_float(val, default=3.0) == 3.0


# ══ _feed_resolved_price ═════════════════════════════════════════════════════

class TestFeedResolvedPrice:
    def test_non_dict_is_zero(self):
        assert gw._feed_resolved_price(None) == 0.0
        assert gw._feed_resolved_price("x") == 0.0

    def test_first_tier_is_data_feed_payload_price(self, monkeypatch):
        monkeypatch.setattr(data_feed, "_payload_price", lambda d: 12.5)
        monkeypatch.setattr(price_resolver, "resolve_display_price", lambda *a, **k: 99.0)
        assert gw._feed_resolved_price({"price": 1}) == 12.5

    def test_second_tier_is_price_resolver_with_symbol_and_feed(self, monkeypatch):
        seen = []
        monkeypatch.setattr(data_feed, "_payload_price", lambda d: 0)

        def resolver(symbol, tick, feed):
            seen.append((symbol, tick, feed))
            return "33.5"

        monkeypatch.setattr(price_resolver, "resolve_display_price", resolver)
        data = {"symbol": "TCS", "x": 1}
        assert gw._feed_resolved_price(data) == 33.5
        assert seen == [("TCS", {}, data)]

    def test_missing_symbol_is_passed_as_empty_string(self, monkeypatch):
        seen = []
        monkeypatch.setattr(data_feed, "_payload_price", lambda d: 0)
        monkeypatch.setattr(price_resolver, "resolve_display_price",
                            lambda symbol, tick, feed: seen.append(symbol) or 4.0)
        assert gw._feed_resolved_price({}) == 4.0 and seen == [""]

    def test_first_tier_exception_falls_to_resolver(self, monkeypatch):
        def boom(d):
            raise RuntimeError("x")

        monkeypatch.setattr(data_feed, "_payload_price", boom)
        monkeypatch.setattr(price_resolver, "resolve_display_price", lambda *a, **k: 8.0)
        assert gw._feed_resolved_price({}) == 8.0

    @pytest.fixture
    def local_only(self, monkeypatch):
        monkeypatch.setattr(data_feed, "_payload_price", lambda d: 0)
        monkeypatch.setattr(price_resolver, "resolve_display_price", lambda *a, **k: 0)

    def test_resolver_exception_falls_to_local_scan(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")

        monkeypatch.setattr(data_feed, "_payload_price", lambda d: 0)
        monkeypatch.setattr(price_resolver, "resolve_display_price", boom)
        assert gw._feed_resolved_price({"close": "7"}) == 7.0

    def test_local_scan_key_priority(self, local_only):
        d = {"prev_close": 1, "cmp": 2, "ltp": 3, "close": 4, "price": 5}
        assert gw._feed_resolved_price(d) == 5.0
        assert gw._feed_resolved_price({"prev_close": 1, "cmp": 2, "ltp": 3, "close": 4}) == 4.0
        assert gw._feed_resolved_price({"prev_close": 1, "cmp": 2, "ltp": 3}) == 3.0
        assert gw._feed_resolved_price({"prev_close": 1, "cmp": 2}) == 2.0
        assert gw._feed_resolved_price({"prev_close": 1, "last_price": 6}) == 6.0
        assert gw._feed_resolved_price({"prev_close": 1}) == 1.0

    def test_local_scan_parses_commas_and_spaces(self, local_only):
        assert gw._feed_resolved_price({"price": "1,234.5"}) == 1234.5
        assert gw._feed_resolved_price({"price": " 9 9 "}) == 99.0

    def test_local_scan_skips_none_empty_sentinels_zero_and_garbage(self, local_only):
        d = {"price": None, "close": "", "ltp": "N/A", "cmp": "-", "last_price": "abc", "prev_close": 0}
        assert gw._feed_resolved_price(d) == 0.0

    def test_local_scan_skips_a_zero_string_for_the_next_key(self, local_only):
        assert gw._feed_resolved_price({"price": "0", "close": 5}) == 5.0

    def test_local_scan_falls_through_a_bad_value_to_the_next_key(self, local_only):
        assert gw._feed_resolved_price({"price": "abc", "close": 0, "ltp": -4, "cmp": "2"}) == 2.0

    def test_local_scan_reads_nested_metrics_when_flat_key_is_blank(self, local_only):
        assert gw._feed_resolved_price({"price": "", "metrics": {"price": 3, "close": 8}}) == 3.0
        assert gw._feed_resolved_price({"price": None, "metrics": {"close": 8}}) == 8.0

    def test_flat_zero_falls_back_to_metrics_for_the_same_key(self, local_only):
        # FIXED: `0 not in (None, "")` used to make the flat 0 win, so metrics was never consulted for that
        # key. A flat value that is zero / negative / junk now falls through to the metrics value.
        assert gw._feed_resolved_price({"price": 0, "metrics": {"price": 55}}) == 55.0
        assert gw._feed_resolved_price({"price": "-", "metrics": {"price": "1,200"}}) == 1200.0
        assert gw._feed_resolved_price({"price": "abc", "metrics": {"price": 7}}) == 7.0
        assert gw._feed_resolved_price({"price": -4, "metrics": {"price": 9}}) == 9.0

    def test_a_positive_flat_value_still_beats_metrics(self, local_only):
        assert gw._feed_resolved_price({"price": 3, "metrics": {"price": 55}}) == 3.0

    def test_flat_key_order_still_beats_a_later_metrics_key(self, local_only):
        # key order is price, close, ...: metrics.price is consulted before the flat `close`
        assert gw._feed_resolved_price({"price": 0, "close": 8, "metrics": {"price": 55}}) == 55.0
        assert gw._feed_resolved_price({"price": 0, "close": 8, "metrics": {"close": 99}}) == 8.0

    def test_infinite_price_is_never_returned(self, local_only):
        assert gw._feed_resolved_price({"price": "inf", "close": 6}) == 6.0

    def test_non_dict_metrics_is_ignored(self, local_only):
        assert gw._feed_resolved_price({"metrics": "x"}) == 0.0

    def test_real_helpers_end_to_end(self):
        assert gw._feed_resolved_price({"price": "1,500"}) == 1500.0
        assert gw._feed_resolved_price({"metrics": {"close": 20}}) == 20.0
        assert gw._feed_resolved_price({}) == 0.0


# ══ _feed_missing_fields (sits between the two helpers above) ════════════════

class TestFeedMissingFields:
    FULL = {"price": 10, "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0}

    def test_complete_row_has_nothing_missing(self):
        assert gw._feed_missing_fields(dict(self.FULL)) == []

    def test_non_dict_misses_everything(self):
        assert gw._feed_missing_fields(None) == ["price", "rsi", "pe_ratio", "roce", "sentiment_score"]

    def test_zero_or_none_numeric_fields_are_missing_but_zero_sentiment_is_not(self):
        row = {**self.FULL, "rsi": 0, "pe_ratio": None, "roce": "0"}
        assert gw._feed_missing_fields(row) == ["rsi", "pe_ratio", "roce"]

    def test_none_sentiment_is_missing(self):
        assert gw._feed_missing_fields({**self.FULL, "sentiment_score": None}) == ["sentiment_score"]

    def test_nested_metrics_are_read(self):
        row = {"price": 5, "metrics": {"rsi": 40, "pe": 12, "roce": 9, "sentiment_score": 1}}
        assert gw._feed_missing_fields(row) == []

    def test_alternate_pe_and_news_score_keys(self):
        row = {"price": 5, "rsi": 1, "pe": 12, "roce": 9, "news_score": 0.3}
        assert gw._feed_missing_fields(row) == []

    def test_flat_keys_win_over_metrics(self):
        row = {**self.FULL, "rsi": 0, "metrics": {"rsi": 70}}
        assert gw._feed_missing_fields(row) == ["rsi"]
