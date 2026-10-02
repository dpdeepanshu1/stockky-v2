"""tests/test_main_patch_single_stock_feed.py — coverage for api-gateway/main.py, slice 18 (lines 10337-10722)

Pass 76. `_patch_single_stock_feed(symbol, client)`, the surgical repair worker behind every Repair button:

* four "purge without a network call" short-circuits: static known-high-price, stored over-cap row,
  known-delisted, self-learned delisted (each with its import-failure fallback and a swallowed delete error);
* the price tier: `/quote/{sym}` with the `source: failed` / empty-price / 404 self-heal through
  `resolve_with_fallback` (purge on unresolved, retry under the renamed ticker), over-cap purge on a live quote,
  OHLCV + volume merge, 401/429 back-off;
* RSI: local yfinance 1mo history, then the technical service (404 -> `/technical/{sym}` fallback);
* PE / ROCE from the fundamental service (404 fallback, metrics merge, never-overwrite rules);
* sentiment from the news service;
* the baseline seeds (RSI 50, PE 22.5, ROCE 15, sentiment 0.65) and the final envelope / persisted row.

Everything downstream is faked: the feed store, the shared async httpx client, `symbol_aliases` predicates,
`yfinance.Ticker`, `data_feed.compute_rsi_from_closes`, `asyncio.sleep`. Nothing touches the network or a
database. Findings are pinned as current behaviour and marked ``NOT FIXED``; the ones fixed afterwards (unusable price
aliases, a stored `news_score: None`) now pin the fixed behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_patch_single_stock_feed.py -v
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
import symbol_aliases
import yfinance


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, data=None, json_raises=False):
        self.status_code, self._data, self._raises = status, data, json_raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._data


class FakeClient:
    """`routes` maps a URL substring -> FakeResp | Exception | list (consumed in order, last one sticks)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        for frag, out in self.routes.items():
            if frag in url:
                if isinstance(out, list):
                    out = out.pop(0) if len(out) > 1 else out[0]
                if isinstance(out, Exception):
                    raise out
                return out
        return FakeResp(404)

    def urls(self):
        return [u for u, _ in self.calls]


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.deleted = []
        self.puts = []
        self.delete_raises = False

    def get_symbol(self, sym):
        return self.rows.get(sym)

    def delete_symbol(self, sym):
        if self.delete_raises:
            raise RuntimeError("delete failed")
        self.deleted.append(sym)

    def put_symbol(self, sym, row, ttl=None):
        self.puts.append((sym, row, ttl))


class FakeHist:
    def __init__(self, closes, empty=False, has_close=True):
        self.empty = empty
        self.columns = ["Close"] if has_close else ["Open"]
        self._closes = closes

    def __getitem__(self, key):
        assert key == "Close"
        return types.SimpleNamespace(dropna=lambda: types.SimpleNamespace(values=self._closes))


@pytest.fixture
def env(monkeypatch):
    """Store, client, sleeps, alias predicates, yfinance and the four service URLs — all faked."""
    e = types.SimpleNamespace(
        store=FakeStore(), client=FakeClient(), sleeps=[],
        high=set(), delisted=set(), learned=set(),
        fallback_calls=[], fallback=lambda sym: (sym + ".NS", {"resolution": "unchanged"}),
        hist=None, hist_raises=False, yf_calls=[], rsi_value=61.5, rsi_calls=[],
        ticker="TICK.NS",
    )

    async def fake_sleep(d):
        e.sleeps.append(d)

    class FakeTicker:
        def __init__(self, t):
            e.yf_calls.append(t)

        def history(self, period=None):
            if e.hist_raises:
                raise RuntimeError("yahoo down")
            return e.hist

    def rsi(closes, period=14):
        e.rsi_calls.append((list(closes), period))
        return e.rsi_value

    def resolve_fb(sym):
        e.fallback_calls.append(sym)
        return e.fallback(sym)

    monkeypatch.setattr(gw, "_feed_store", lambda: e.store)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
    monkeypatch.setattr(gw, "REPAIR_COOLDOWN_SEC", 0.5)
    monkeypatch.setattr(gw, "resolve_ns_ticker", lambda b: e.ticker)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(symbol_aliases, "is_known_high_price", lambda s: s in e.high)
    monkeypatch.setattr(symbol_aliases, "is_known_delisted", lambda s: s in e.delisted)
    monkeypatch.setattr(symbol_aliases, "is_learned_delisted", lambda s: s in e.learned)
    monkeypatch.setattr(symbol_aliases, "MAX_FAILURE_STREAK", 7)
    monkeypatch.setattr(symbol_aliases, "resolve_with_fallback", resolve_fb)
    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    monkeypatch.setattr(data_feed, "compute_rsi_from_closes", rsi)
    for k, v in (("MARKET_DATA_URL", "http://md.t"), ("TECHNICAL_URL", "http://tech.t"),
                 ("FUNDAMENTAL_URL", "http://fund.t"), ("NEWS_URL", "http://news.t")):
        monkeypatch.setenv(k, v)
    e.mp = monkeypatch
    return e


def go(env, symbol="TCS", row=None):
    if row is not None:
        env.store.rows[symbol.upper().replace(".NS", "").replace(".BO", "").strip()] = row
    return _run(gw._patch_single_stock_feed(symbol, env.client))


def stored(env):
    assert len(env.store.puts) == 1
    return env.store.puts[0][1]


# a row with everything present except `price`
NO_PRICE = {"rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0.3}
# a row with everything present
FULL = {**NO_PRICE, "price": 100}


# ══ purge short-circuits ═════════════════════════════════════════════════════

class TestKnownHighPrice:
    def test_purged_without_any_network_call(self, env):
        env.high.add("MRF")
        out = go(env, "mrf.ns", {"price": 4000})
        assert out == {"symbol": "MRF", "patched_fields": [], "still_missing": [], "price": 4000.0,
                       "complete": False, "purged": True,
                       "message": "MRF is a known ₹5000+ stock — removed from feed without a network call."}
        assert env.store.deleted == ["MRF"] and env.client.calls == [] and env.store.puts == []

    def test_not_purged_when_no_cap_is_configured(self, env):
        env.mp.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        env.high.add("MRF")
        out = go(env, "MRF", dict(FULL))
        assert "purged" not in out and env.store.deleted == []

    def test_delete_failure_is_swallowed(self, env):
        env.high.add("MRF")
        env.store.delete_raises = True
        assert go(env, "MRF")["purged"] is True

    def test_missing_row_prices_as_zero(self, env):
        env.high.add("MRF")
        assert go(env, "MRF")["price"] == 0.0

    def test_predicate_import_failure_means_no_purge(self, env):
        env.mp.delattr(symbol_aliases, "is_known_high_price")
        out = go(env, "MRF", dict(FULL))
        assert "purged" not in out and env.store.deleted == []

    def test_symbol_is_normalised(self, env):
        env.high.add("INFY")
        assert go(env, " infy.bo ")["symbol"] == "INFY"

    def test_none_symbol_becomes_empty(self, env):
        out = _run(gw._patch_single_stock_feed(None, env.client))
        assert out["symbol"] == ""


class TestOverCapRow:
    def test_stored_over_cap_row_is_purged(self, env):
        out = go(env, "BIG", {"price": 9000})
        assert out["purged"] is True and out["price"] == 9000.0 and out["complete"] is False
        assert out["message"] == "BIG was above ₹5000 cap — removed from feed instead of repaired."
        assert env.store.deleted == ["BIG"] and env.client.calls == []

    def test_under_cap_row_is_not_purged(self, env):
        out = go(env, "OK", dict(FULL))
        assert "purged" not in out

    def test_empty_row_skips_the_over_cap_check(self, env):
        out = go(env, "NEW")
        assert "purged" not in out

    def test_delete_failure_is_swallowed(self, env):
        env.store.delete_raises = True
        assert go(env, "BIG", {"price": 9000})["purged"] is True

    def test_no_cap_means_no_purge(self, env):
        env.mp.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        assert "purged" not in go(env, "BIG", {**NO_PRICE, "price": 9000})


class TestDelisted:
    def test_known_delisted_is_purged(self, env):
        env.delisted.add("OLD")
        out = go(env, "OLD", {"price": 10})
        assert out["purged"] is True and out["price"] == 10.0
        assert out["message"] == "OLD is delisted/merged (not a rename) — removed from feed without a network call."
        assert env.store.deleted == ["OLD"] and env.client.calls == []

    def test_known_delisted_ignores_the_cap_setting(self, env):
        env.mp.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        env.delisted.add("OLD")
        assert go(env, "OLD")["purged"] is True

    def test_known_delisted_delete_failure_is_swallowed(self, env):
        env.delisted.add("OLD")
        env.store.delete_raises = True
        assert go(env, "OLD")["purged"] is True

    def test_learned_delisted_is_purged_with_the_streak_in_the_message(self, env):
        env.learned.add("GHOST")
        out = go(env, "GHOST")
        assert out["purged"] is True and env.client.calls == []
        assert out["message"] == ("GHOST failed to resolve on 7+ consecutive attempts across all price sources — "
                                  "removed from feed as probably delisted (self-learned, not manually confirmed).")

    def test_learned_delisted_delete_failure_is_swallowed(self, env):
        env.learned.add("GHOST")
        env.store.delete_raises = True
        assert go(env, "GHOST")["purged"] is True

    def test_known_beats_learned(self, env):
        env.delisted.add("X")
        env.learned.add("X")
        assert "delisted/merged" in go(env, "X")["message"]

    def test_over_cap_beats_delisted(self, env):
        env.delisted.add("X")
        assert "above ₹5000 cap" in go(env, "X", {"price": 9000})["message"]

    def test_predicate_import_failure_means_no_purge_and_no_fallback_resolver(self, env):
        env.delisted.add("OLD")
        env.mp.delattr(symbol_aliases, "is_known_delisted")
        env.client.routes["/quote/"] = FakeResp(404)
        out = go(env, "OLD", dict(NO_PRICE))
        assert "purged" not in out and env.fallback_calls == []        # _resolve_with_fallback is None
        assert env.store.deleted == [] and out["still_missing"] == ["price"]


# ══ price tier ═══════════════════════════════════════════════════════════════

class TestPriceTier:
    def test_quote_is_fetched_with_8s_timeout_and_merged(self, env):
        env.client.routes["/quote/TCS"] = FakeResp(200, {"price": 101.5, "volume": 1500.7,
                                                         "previous_close": 99, "day_high": 102, "day_low": 98,
                                                         "day_change_pct": 1.5, "junk": None})
        out = go(env, "TCS", dict(NO_PRICE))
        assert env.client.calls == [("http://md.t/quote/TCS", 8.0)]
        row = stored(env)
        assert row["price"] == 101.5 and row["close"] == 101.5 and row["cmp"] == 101.5 and row["ltp"] == 101.5
        assert row["volume"] == 1500 and row["previous_close"] == 99 and row["day_high"] == 102
        assert row["day_low"] == 98 and row["day_change_pct"] == 1.5 and "junk" not in row
        assert out["patched_fields"] == ["price"] and out["price"] == 101.5 and out["complete"] is True
        assert out["message"] == "Repaired TCS: price — complete"
        assert env.sleeps == [0.5]

    def test_unusable_aliases_are_replaced_by_the_repaired_price(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 50})
        go(env, "TCS", {**NO_PRICE, "close": 0, "cmp": "", "ltp": None})
        row = stored(env)
        # FIXED: setdefault used to keep a stored 0 / "" / None, so the aliases stayed unusable next to
        # price=50. A stored alias that is not a positive number is now overwritten with the repaired price.
        assert row["price"] == 50.0 and row["close"] == 50.0 and row["cmp"] == 50.0 and row["ltp"] == 50.0

    @pytest.mark.parametrize("bad", ["-", "abc", -3, "nan", "0"])
    def test_other_unusable_alias_values_are_replaced_too(self, env, bad):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 50})
        go(env, "TCS", {**NO_PRICE, "close": bad, "cmp": bad, "ltp": bad})
        row = stored(env)
        assert row["close"] == 50.0 and row["cmp"] == 50.0 and row["ltp"] == 50.0

    def test_valid_existing_aliases_are_still_kept(self, env, monkeypatch):
        # A usable alias normally means the price is not "missing" at all (the resolver reads the same
        # keys), so force the price tier to run to prove the keep-if-valid branch directly.
        monkeypatch.setattr(gw, "_feed_missing_fields", lambda d: ["price"])
        env.client.routes["/quote/"] = FakeResp(200, {"price": 50})
        go(env, "TCS", {**NO_PRICE, "close": 48.5, "cmp": "49", "ltp": 0})
        row = stored(env)
        # never wipes a valid existing value; only the unusable `ltp` is repaired
        assert row["price"] == 50.0 and row["close"] == 48.5 and row["cmp"] == "49" and row["ltp"] == 50.0

    def test_key_priority_skips_none_and_zero(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": None, "cmp": 0, "ltp": "1,234.5", "close": 9})
        go(env, "TCS", dict(NO_PRICE))
        assert stored(env)["price"] == 1234.5

    def test_price_key_wins_over_cmp(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 10, "cmp": 20, "ltp": 30})
        go(env, "A", dict(NO_PRICE))
        assert stored(env)["price"] == 10.0

    def test_last_price_and_regular_market_price_keys(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"regularMarketPrice": 7})
        go(env, "A", dict(NO_PRICE))
        assert stored(env)["price"] == 7.0

    @pytest.mark.parametrize("vol", [0, -5, "1,000", "abc", None])
    def test_unusable_volume_is_ignored(self, env, vol):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 10, "volume": vol})
        go(env, "A", dict(NO_PRICE))
        assert "volume" not in stored(env)

    def test_zero_price_quote_is_not_patched_and_price_stays_missing(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 0, "cmp": 0})
        env.fallback = lambda s: (s + ".NS", {"resolution": "unchanged"})
        out = go(env, "A", dict(NO_PRICE))
        assert out["patched_fields"] == [] and out["still_missing"] == ["price"] and out["complete"] is False
        assert out["message"] == "Repaired A: no changes — still missing ['price']"

    def test_price_not_missing_means_no_quote_call(self, env):
        go(env, "A", dict(FULL))
        assert env.client.calls == [] and env.sleeps == []

    def test_blank_market_url_skips_the_tier(self, env):
        env.mp.setenv("MARKET_DATA_URL", "")
        env.mp.setattr(gw, "MARKET_DATA_URL", "")
        out = go(env, "A", dict(NO_PRICE))
        assert env.client.calls == [] and out["still_missing"] == ["price"]

    def test_market_url_falls_back_to_the_module_constant_and_strips_the_slash(self, env):
        env.mp.delenv("MARKET_DATA_URL")
        env.mp.setattr(gw, "MARKET_DATA_URL", "http://const.t/")
        env.client.routes["/quote/"] = FakeResp(200, {"price": 5})
        go(env, "A", dict(NO_PRICE))
        assert env.client.urls() == ["http://const.t/quote/A"]

    # -- over-cap live quote ---------------------------------------------------
    def test_over_cap_live_quote_purges(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 5000.01})
        out = go(env, "A", dict(NO_PRICE))
        assert out == {"symbol": "A", "patched_fields": [], "still_missing": [], "price": 5000.01,
                       "complete": False, "purged": True,
                       "message": "A is above ₹5000 cap — removed from feed."}
        assert env.store.deleted == ["A"] and env.store.puts == []

    def test_price_exactly_at_cap_is_kept(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 5000})
        assert "purged" not in go(env, "A", dict(NO_PRICE))

    def test_over_cap_purge_delete_failure_is_swallowed(self, env):
        env.store.delete_raises = True
        env.client.routes["/quote/"] = FakeResp(200, {"price": 9000})
        assert go(env, "A", dict(NO_PRICE))["purged"] is True

    def test_no_cap_keeps_a_huge_price(self, env):
        env.mp.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
        env.client.routes["/quote/"] = FakeResp(200, {"price": 99999})
        out = go(env, "A", dict(NO_PRICE))
        assert out["price"] == 99999.0 and "purged" not in out

    # -- fallback self-heal ----------------------------------------------------
    def test_source_failed_body_triggers_fallback_and_retries_under_the_new_ticker(self, env):
        env.client.routes["/quote/OLDNAME"] = FakeResp(200, {"price": None, "source": "failed"})
        env.client.routes["/quote/NEWNAME"] = FakeResp(200, {"price": 77})
        env.fallback = lambda s: ("NEWNAME.NS", {"resolution": "static_rename"})
        out = go(env, "OLDNAME", dict(NO_PRICE))
        assert env.fallback_calls == ["OLDNAME"]
        assert env.client.urls() == ["http://md.t/quote/OLDNAME", "http://md.t/quote/NEWNAME"]
        assert out["patched_fields"] == ["price"] and stored(env)["price"] == 77.0
        assert stored(env)["symbol"] == "OLDNAME"          # stored under the original symbol

    def test_failed_source_with_a_price_still_counts_as_failed(self, env):
        env.client.routes["/quote/A"] = FakeResp(200, {"price": 10, "source": "failed"})
        env.client.routes["/quote/B"] = FakeResp(200, {"price": 33})
        env.fallback = lambda s: ("B.BO", {"resolution": "x"})
        go(env, "A", dict(NO_PRICE))
        assert env.client.urls()[-1] == "http://md.t/quote/B" and stored(env)["price"] == 33.0

    def test_empty_price_body_triggers_fallback(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 0})
        go(env, "A", dict(NO_PRICE))
        assert env.fallback_calls == ["A"]

    def test_unparseable_200_body_triggers_fallback(self, env):
        env.client.routes["/quote/"] = FakeResp(200, json_raises=True)
        go(env, "A", dict(NO_PRICE))
        assert env.fallback_calls == ["A"]

    def test_404_triggers_fallback(self, env):
        env.client.routes["/quote/"] = FakeResp(404)
        go(env, "A", dict(NO_PRICE))
        assert env.fallback_calls == ["A"]

    def test_healthy_quote_does_not_trigger_fallback(self, env):
        env.client.routes["/quote/"] = FakeResp(200, {"price": 5})
        go(env, "A", dict(NO_PRICE))
        assert env.fallback_calls == []

    def test_non_dict_200_body_is_not_a_failure_and_patches_nothing(self, env):
        env.client.routes["/quote/"] = FakeResp(200, [1, 2])
        out = go(env, "A", dict(NO_PRICE))
        assert env.fallback_calls == [] and out["patched_fields"] == []

    def test_unchanged_ticker_means_no_retry(self, env):
        env.client.routes["/quote/"] = FakeResp(404)
        env.fallback = lambda s: ("A.NS", {"resolution": "unchanged"})
        go(env, "A", dict(NO_PRICE))
        assert len(env.client.calls) == 1

    def test_unresolved_symbol_is_purged_with_the_resolution_in_the_message(self, env):
        env.client.routes["/quote/"] = FakeResp(404)
        env.fallback = lambda s: (None, {"resolution": "skip_not_nse"})
        out = go(env, "A", dict(NO_PRICE))
        assert out == {"symbol": "A", "patched_fields": [], "still_missing": [], "price": 0.0,
                       "complete": False, "purged": True, "message": "A: skip_not_nse — removed from feed."}
        assert env.store.deleted == ["A"] and env.store.puts == []

    def test_unresolved_without_a_resolution_key_says_unresolved(self, env):
        env.client.routes["/quote/"] = FakeResp(404)
        env.fallback = lambda s: (None, {})
        assert go(env, "A", dict(NO_PRICE))["message"] == "A: unresolved — removed from feed."

    def test_unresolved_purge_delete_failure_is_swallowed(self, env):
        env.store.delete_raises = True
        env.client.routes["/quote/"] = FakeResp(404)
        env.fallback = lambda s: (None, {})
        assert go(env, "A", dict(NO_PRICE))["purged"] is True

    def test_retry_that_is_also_over_cap_purges(self, env):
        env.client.routes["/quote/A"] = FakeResp(404)
        env.client.routes["/quote/B"] = FakeResp(200, {"price": 9000})
        env.fallback = lambda s: ("B.NS", {"resolution": "x"})
        assert go(env, "A", dict(NO_PRICE))["purged"] is True

    def test_fallback_exception_is_swallowed_and_patching_continues(self, env):
        env.client.routes["/quote/"] = FakeResp(404)

        def boom(s):
            raise RuntimeError("alias table down")

        env.fallback = boom
        out = go(env, "A", dict(NO_PRICE))
        assert out["still_missing"] == ["price"] and env.sleeps == [0.5]

    # -- errors / back-off -----------------------------------------------------
    @pytest.mark.parametrize("status", [401, 429])
    def test_auth_and_rate_limit_back_off_double_then_cooldown(self, env, status):
        env.client.routes["/quote/"] = FakeResp(status)
        env.fallback = lambda s: ("A.NS", {})
        go(env, "A", dict(NO_PRICE))
        assert env.sleeps == [1.0, 0.5]

    def test_other_status_just_cools_down(self, env):
        env.client.routes["/quote/"] = FakeResp(500)
        go(env, "A", dict(NO_PRICE))
        assert env.sleeps == [0.5]

    def test_transport_exception_is_swallowed_and_cools_down(self, env):
        env.client.routes["/quote/"] = RuntimeError("conn reset")
        out = go(env, "A", dict(NO_PRICE))
        assert env.sleeps == [0.5] and out["still_missing"] == ["price"]

    def test_cooldown_is_floored_at_100ms(self, env):
        env.mp.setattr(gw, "REPAIR_COOLDOWN_SEC", 0.0)
        env.client.routes["/quote/"] = FakeResp(500)
        go(env, "A", dict(NO_PRICE))
        assert env.sleeps == [0.1]

    def test_cooldown_follows_the_configured_value(self, env):
        env.mp.setattr(gw, "REPAIR_COOLDOWN_SEC", 2.0)
        env.client.routes["/quote/"] = FakeResp(429)
        env.fallback = lambda s: ("A.NS", {})
        go(env, "A", dict(NO_PRICE))
        assert env.sleeps == [4.0, 2.0]


# ══ RSI tier ═════════════════════════════════════════════════════════════════

NO_RSI = {"price": 100, "pe_ratio": 20, "roce": 15, "sentiment_score": 0.3}


class TestRsiTier:
    def test_local_yahoo_history_wins_and_skips_the_technical_service(self, env):
        env.hist = FakeHist([1.0, 2.0, 3.0])
        out = go(env, "A", dict(NO_RSI))
        assert env.yf_calls == ["TICK.NS"] and env.rsi_calls == [([1.0, 2.0, 3.0], 14)]
        assert stored(env)["rsi"] == 61.5 and "rsi_seed" not in stored(env)
        assert out["patched_fields"] == ["rsi"] and env.client.calls == [] and env.sleeps == []

    def test_no_ticker_skips_yahoo(self, env):
        env.ticker = None
        go(env, "A", dict(NO_RSI))
        assert env.yf_calls == []

    @pytest.mark.parametrize("hist", [None, FakeHist([1], empty=True), FakeHist([1], has_close=False)])
    def test_unusable_history_falls_through(self, env, hist):
        env.hist = hist
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 44})
        go(env, "A", dict(NO_RSI))
        assert env.rsi_calls == [] and stored(env)["rsi"] == 44.0

    def test_rsi_none_from_history_falls_through_to_the_technical_service(self, env):
        env.hist = FakeHist([1.0])
        env.rsi_value = None
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 33})
        go(env, "A", dict(NO_RSI))
        assert stored(env)["rsi"] == 33.0

    def test_yahoo_exception_is_swallowed(self, env):
        env.hist_raises = True
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 33})
        assert go(env, "A", dict(NO_RSI))["patched_fields"] == ["rsi"]

    def test_technical_service_fields_are_merged(self, env):
        env.hist = None
        env.client.routes["/analyze/A"] = FakeResp(200, {"rsi": 44, "ema20": 99, "technical_score": 7,
                                                         "macd_hist": 0.5, "macd": 9})
        go(env, "A", dict(NO_RSI))
        assert env.client.calls == [("http://tech.t/analyze/A?lite=1", 20.0)]
        row = stored(env)
        assert row["rsi"] == 44.0 and row["ema20"] == 99 and row["technical_score"] == 7
        assert row["macd_hist"] == 0.5

    def test_macd_is_the_fallback_for_macd_hist(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 44, "macd": 9})
        go(env, "A", dict(NO_RSI))
        assert stored(env)["macd_hist"] == 9

    def test_existing_technical_score_is_not_overwritten(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 44, "technical_score": 7})
        go(env, "A", {**NO_RSI, "technical_score": 3})
        assert stored(env)["technical_score"] == 3

    def test_404_falls_back_to_the_technical_endpoint(self, env):
        env.client.routes["/analyze/"] = FakeResp(404)
        env.client.routes["/technical/A"] = FakeResp(200, {"rsi": 55})
        go(env, "A", dict(NO_RSI))
        assert env.client.urls() == ["http://tech.t/analyze/A?lite=1", "http://tech.t/technical/A"]
        assert stored(env)["rsi"] == 55.0

    @pytest.mark.parametrize("rsi", [None, 0, "0", "-"])
    def test_zero_or_missing_rsi_is_not_patched_and_gets_the_seed(self, env, rsi):
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": rsi})
        go(env, "A", dict(NO_RSI))
        assert stored(env)["rsi"] == 50.0 and stored(env)["rsi_seed"] is True

    def test_non_dict_technical_body_is_empty(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, [1])
        go(env, "A", dict(NO_RSI))
        assert stored(env)["rsi_seed"] is True

    @pytest.mark.parametrize("status", [401, 429])
    def test_back_off_on_auth_and_rate_limit(self, env, status):
        env.client.routes["/analyze/"] = FakeResp(status)
        go(env, "A", dict(NO_RSI))
        assert env.sleeps == [1.0, 0.5]

    def test_transport_error_is_swallowed(self, env):
        env.client.routes["/analyze/"] = RuntimeError("down")
        assert go(env, "A", dict(NO_RSI))["patched_fields"] == ["rsi"]       # via the seed
        assert env.sleeps == [0.5]

    def test_blank_technical_url_skips_the_service(self, env):
        env.mp.setenv("TECHNICAL_URL", "")
        env.mp.setattr(gw, "TECHNICAL_URL", "")
        go(env, "A", dict(NO_RSI))
        assert env.client.calls == [] and stored(env)["rsi_seed"] is True

    def test_technical_url_falls_back_to_the_constant(self, env):
        env.mp.delenv("TECHNICAL_URL")
        env.mp.setattr(gw, "TECHNICAL_URL", "http://tconst.t/")
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 40})
        go(env, "A", dict(NO_RSI))
        assert env.client.urls() == ["http://tconst.t/analyze/A?lite=1"]

    def test_technical_success_does_not_flag_the_real_rsi_as_a_seed(self, env):
        """FIXED: the technical-service branch now does `missing.discard("rsi")`, so the baseline-seed block
        no longer fires and the real RSI is not stamped `rsi_seed=True`."""
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 44})
        out = go(env, "A", dict(NO_RSI))
        assert stored(env)["rsi"] == 44.0 and "rsi_seed" not in stored(env)
        assert out["patched_fields"] == ["rsi"]                 # de-duplicated in the response

    def test_stored_zero_rsi_is_seeded(self, env):
        """FIXED: a stored 0 / None RSI is "unusable", so the seed now replaces it."""
        env.client.routes["/analyze/"] = FakeResp(404)
        out = go(env, "A", {**NO_RSI, "rsi": 0})
        assert stored(env)["rsi"] == 50.0 and stored(env)["rsi_seed"] is True
        assert out["still_missing"] == [] and out["complete"] is True


# ══ fundamental tier ═════════════════════════════════════════════════════════

NO_PE_ROCE = {"price": 100, "rsi": 50, "sentiment_score": 0.3}


class TestFundamentalTier:
    def test_pe_and_roce_requested_with_skip_peers_and_35s_timeout(self, env):
        env.client.routes["/analyze/A"] = FakeResp(200, {"pe_ratio": 18, "roce": 21})
        go(env, "A", dict(NO_PE_ROCE))
        assert env.client.calls == [("http://fund.t/analyze/A?skip_peers=1&lite=1", 35.0)]

    def test_404_falls_back_to_the_fundamental_endpoint(self, env):
        env.client.routes["/analyze/"] = FakeResp(404)
        env.client.routes["/fundamental/A"] = FakeResp(200, {"pe": 18})
        go(env, "A", dict(NO_PE_ROCE))
        assert env.client.urls() == ["http://fund.t/analyze/A?skip_peers=1&lite=1",
                                     "http://fund.t/fundamental/A?skip_peers=1"]
        assert env.client.calls[1][1] == 35.0

    def test_real_pe_and_roce_are_kept_out_of_the_baseline_seeds(self, env):
        """FIXED (data-quality bug): the PE / ROCE branches now `missing.discard(...)`, so the seed block no
        longer replaces the real values with the flat defaults (PE 22.5, ROCE 15.0)."""
        env.client.routes["/analyze/A"] = FakeResp(200, {"pe_ratio": 18.2, "roce": 31.4,
                                                         "metrics": {"pe_ratio": 18.2, "roce": 31.4}})
        out = go(env, "A", dict(NO_PE_ROCE))
        row = stored(env)
        assert row["pe_ratio"] == 18.2 and "pe_seed" not in row
        assert row["roce"] == 31.4 and "roce_seed" not in row
        assert row["metrics"] == {"pe_ratio": 18.2, "roce": 31.4}
        assert out["patched_fields"] == ["pe_ratio", "roce"] and out["complete"] is True

    @pytest.mark.parametrize("body,field", [
        ({"pe_ratio": 5}, "pe_ratio"), ({"pe": 5}, "pe_ratio"),
        ({"metrics": {"pe_ratio": 5}}, "pe_ratio"), ({"metrics": {"pe": 5}}, "pe_ratio"),
        ({"roce": 5}, "roce"), ({"metrics": {"roce": 5}}, "roce"),
    ])
    def test_every_key_shape_counts_as_a_patch(self, env, body, field):
        env.client.routes["/analyze/"] = FakeResp(200, body)
        go(env, "A", dict(NO_PE_ROCE))
        # FIXED: a real patch no longer re-enters the seed block, so it is listed exactly once
        assert stored(env)["repair_patched"].count(field) == 1

    @pytest.mark.parametrize("val", [None, 0, "0", "NA"])
    def test_zero_or_missing_values_are_not_patched_but_still_seeded(self, env, val):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": val, "roce": val})
        out = go(env, "A", dict(NO_PE_ROCE))
        assert stored(env)["pe_seed"] is True and stored(env)["roce_seed"] is True
        assert out["patched_fields"] == ["pe_ratio", "roce"]          # via the seeds

    def test_only_the_missing_field_is_touched(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 99, "roce": 77})
        go(env, "A", {**NO_PE_ROCE, "pe_ratio": 20})                  # roce missing only
        row = stored(env)
        assert row["pe_ratio"] == 20 and "pe_seed" not in row
        assert row["roce"] == 77 and "roce_seed" not in row

    def test_present_roce_is_not_overwritten_when_only_pe_is_missing(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3, "roce": 77})
        go(env, "A", {**NO_PE_ROCE, "roce": 15})
        assert stored(env)["roce"] == 15 and stored(env)["repair_patched"].count("roce") == 0

    def test_only_pe_missing_still_calls_the_service(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3})
        go(env, "A", {**NO_PE_ROCE, "roce": 15})
        assert len(env.client.calls) == 1

    def test_score_sector_and_metrics_merge_without_overwriting(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {
            "pe_ratio": 3, "fundamental_score": 8, "sector": "IT",
            "metrics": {"a": 1, "b": None, "c": 3}})
        go(env, "A", {**NO_PE_ROCE, "metrics": {"a": 0, "z": 9}})
        row = stored(env)
        assert row["fundamental_score"] == 8 and row["sector"] == "IT"
        assert row["metrics"] == {"a": 1, "z": 9, "c": 3}              # None dropped, existing keys updated

    def test_existing_score_and_sector_are_kept(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3, "fundamental_score": 8, "sector": "IT"})
        go(env, "A", {**NO_PE_ROCE, "fundamental_score": 1, "sector": "Banks"})
        assert stored(env)["fundamental_score"] == 1 and stored(env)["sector"] == "Banks"

    def test_non_dict_current_metrics_is_replaced(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3, "metrics": {"a": 1}})
        go(env, "A", {**NO_PE_ROCE, "metrics": "junk"})
        assert stored(env)["metrics"] == {"a": 1}

    def test_non_dict_bodies_are_empty(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, [1])
        assert go(env, "A", dict(NO_PE_ROCE))["still_missing"] == []

    def test_non_dict_metrics_in_body_is_ignored(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3, "metrics": "junk"})
        go(env, "A", dict(NO_PE_ROCE))
        assert "metrics" not in stored(env)

    @pytest.mark.parametrize("status", [401, 429])
    def test_back_off(self, env, status):
        env.client.routes["/analyze/"] = FakeResp(status)
        go(env, "A", dict(NO_PE_ROCE))
        assert env.sleeps == [1.0, 0.5]

    def test_transport_error_is_swallowed(self, env):
        env.client.routes["/analyze/"] = RuntimeError("down")
        assert go(env, "A", dict(NO_PE_ROCE))["complete"] is True      # seeds fill the gap
        assert env.sleeps == [0.5]

    def test_blank_fundamental_url_skips_the_service(self, env):
        env.mp.setenv("FUNDAMENTAL_URL", "")
        env.mp.setattr(gw, "FUNDAMENTAL_URL", "")
        go(env, "A", dict(NO_PE_ROCE))
        assert env.client.calls == []

    def test_fundamental_url_falls_back_to_the_constant(self, env):
        env.mp.delenv("FUNDAMENTAL_URL")
        env.mp.setattr(gw, "FUNDAMENTAL_URL", "http://fconst.t/")
        env.client.routes["/analyze/"] = FakeResp(200, {"pe_ratio": 3})
        go(env, "A", dict(NO_PE_ROCE))
        assert env.client.urls() == ["http://fconst.t/analyze/A?skip_peers=1&lite=1"]


# ══ sentiment tier ═══════════════════════════════════════════════════════════

NO_SENT = {"price": 100, "rsi": 50, "pe_ratio": 20, "roce": 15}


class TestSentimentTier:
    def test_news_score_is_patched_with_alias(self, env):
        env.client.routes["/analyze/A"] = FakeResp(200, {"news_score": 0.8})
        out = go(env, "A", dict(NO_SENT))
        assert env.client.calls == [("http://news.t/analyze/A", 20.0)]
        row = stored(env)
        assert row["sentiment_score"] == 0.8 and row["news_score"] == 0.8 and "sentiment_seed" not in row
        assert out["patched_fields"] == ["sentiment_score"]

    def test_sentiment_score_key_is_the_fallback(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"sentiment_score": -0.2})
        go(env, "A", dict(NO_SENT))
        assert stored(env)["sentiment_score"] == -0.2

    def test_zero_is_a_valid_score(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": 0})
        go(env, "A", dict(NO_SENT))
        assert stored(env)["sentiment_score"] == 0 and "sentiment_seed" not in stored(env)

    def test_none_news_score_alias_is_filled(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": 0.8})
        go(env, "A", {**NO_SENT, "news_score": None})
        # FIXED: `news_score: None` was a "present" key for setdefault, so the alias stayed None.
        assert stored(env)["news_score"] == 0.8 and stored(env)["sentiment_score"] == 0.8

    def test_a_stored_real_news_score_alias_is_kept(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": 0.8})
        # `sentiment_score: None` makes the tier run; a stored news_score of 0 is a real score, not "absent"
        go(env, "A", {**NO_SENT, "sentiment_score": None, "news_score": 0})
        assert stored(env)["news_score"] == 0 and stored(env)["sentiment_score"] == 0.8

    def test_a_stored_nonzero_news_score_alias_is_kept(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": 0.8})
        go(env, "A", {**NO_SENT, "sentiment_score": None, "news_score": 0.3})
        assert stored(env)["news_score"] == 0.3 and stored(env)["sentiment_score"] == 0.8

    def test_none_news_score_alias_is_filled_by_the_seed_too(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": None})
        go(env, "A", {**NO_SENT, "news_score": None})
        row = stored(env)
        assert row["sentiment_seed"] is True and row["news_score"] == 0.65 and row["sentiment_score"] == 0.65

    def test_none_score_falls_to_the_seed(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": None})
        go(env, "A", dict(NO_SENT))
        row = stored(env)
        assert row["sentiment_score"] == 0.65 and row["sentiment_seed"] is True and row["news_score"] == 0.65

    def test_non_dict_body_falls_to_the_seed(self, env):
        env.client.routes["/analyze/"] = FakeResp(200, [1])
        go(env, "A", dict(NO_SENT))
        assert stored(env)["sentiment_seed"] is True

    @pytest.mark.parametrize("status", [401, 429])
    def test_back_off(self, env, status):
        env.client.routes["/analyze/"] = FakeResp(status)
        go(env, "A", dict(NO_SENT))
        assert env.sleeps == [1.0, 0.5]

    def test_other_status_just_cools_down(self, env):
        env.client.routes["/analyze/"] = FakeResp(500)
        go(env, "A", dict(NO_SENT))
        assert env.sleeps == [0.5]

    def test_transport_error_is_swallowed(self, env):
        env.client.routes["/analyze/"] = RuntimeError("down")
        assert go(env, "A", dict(NO_SENT))["complete"] is True

    def test_blank_news_url_skips_the_service(self, env):
        env.mp.setenv("NEWS_URL", "")
        env.mp.setattr(gw, "NEWS_URL", "")
        go(env, "A", dict(NO_SENT))
        assert env.client.calls == []

    def test_news_url_falls_back_to_the_constant(self, env):
        env.mp.delenv("NEWS_URL")
        env.mp.setattr(gw, "NEWS_URL", "http://nconst.t/")
        env.client.routes["/analyze/"] = FakeResp(200, {"news_score": 1})
        go(env, "A", dict(NO_SENT))
        assert env.client.urls() == ["http://nconst.t/analyze/A"]


# ══ seeds + final envelope ═══════════════════════════════════════════════════

class TestSeedsAndEnvelope:
    @pytest.fixture
    def cold(self, env):
        for k in ("MARKET_DATA_URL", "TECHNICAL_URL", "FUNDAMENTAL_URL", "NEWS_URL"):
            env.mp.setenv(k, "")
            env.mp.setattr(gw, k, "")
        env.hist = None
        return env

    def test_everything_cold_seeds_every_field_but_never_invents_a_price(self, cold):
        out = go(cold, "A", {})
        row = stored(cold)
        assert row["rsi"] == 50.0 and row["rsi_seed"] is True
        assert row["pe_ratio"] == 22.5 and row["pe_seed"] is True
        assert row["roce"] == 15.0 and row["roce_seed"] is True
        assert row["sentiment_score"] == 0.65 and row["sentiment_seed"] is True and row["news_score"] == 0.65
        assert "price" not in row
        assert out["patched_fields"] == ["rsi", "pe_ratio", "roce", "sentiment_score"]
        assert out["still_missing"] == ["price"] and out["complete"] is False and out["price"] == 0.0
        assert out["message"] == ("Repaired A: rsi, pe_ratio, roce, sentiment_score — still missing ['price']")
        assert cold.client.calls == [] and cold.sleeps == []

    def test_seeded_sentiment_keeps_an_existing_news_score(self, cold):
        go(cold, "A", {**FULL, "sentiment_score": None, "news_score": 0.1})
        assert stored(cold)["news_score"] == 0.1 and stored(cold)["sentiment_score"] == 0.65

    def test_complete_row_is_a_no_op_with_a_complete_message(self, env):
        out = go(env, "A", dict(FULL))
        assert out == {"symbol": "A", "patched_fields": [], "still_missing": [], "price": 100.0,
                       "complete": True, "message": "Repaired A: no changes — complete"}
        assert env.client.calls == [] and env.sleeps == []

    def test_persisted_row_metadata(self, env):
        go(env, "a.ns", dict(FULL))
        sym, row, ttl = env.store.puts[0]
        assert sym == "A" and ttl == gw.DATA_FEED_TTL
        assert row["symbol"] == "A" and row["repair_patched"] == []
        assert row["repair_updated_at"].endswith("+05:30")

    def test_existing_row_is_copied_not_mutated_in_place(self, env):
        original = dict(NO_RSI)
        env.store.rows["A"] = original
        env.hist = None
        env.mp.setenv("TECHNICAL_URL", "")
        env.mp.setattr(gw, "TECHNICAL_URL", "")
        _run(gw._patch_single_stock_feed("A", env.client))
        assert original == NO_RSI

    def test_stored_repair_patched_is_deduped_like_the_response(self, env):
        """FIXED: the persisted list is assigned AFTER the de-dupe, so it matches the response."""
        env.client.routes["/analyze/"] = FakeResp(200, {"rsi": 44})
        out = go(env, "A", dict(NO_RSI))
        assert stored(env)["repair_patched"] == ["rsi"]
        assert out["patched_fields"] == ["rsi"]

    def test_full_cold_repair_walks_every_tier_in_order(self, env):
        env.hist = FakeHist([1.0, 2.0])
        env.client.routes["/quote/"] = FakeResp(200, {"price": 55, "volume": 10})
        env.client.routes["http://fund.t"] = FakeResp(200, {"pe_ratio": 18, "roce": 20})
        env.client.routes["http://news.t"] = FakeResp(200, {"news_score": 0.4})
        out = go(env, "A", {})
        assert env.client.urls() == ["http://md.t/quote/A",
                                     "http://fund.t/analyze/A?skip_peers=1&lite=1",
                                     "http://news.t/analyze/A"]
        assert env.sleeps == [0.5, 0.5, 0.5]
        assert out["patched_fields"] == ["price", "rsi", "pe_ratio", "roce", "sentiment_score"]
        assert out["complete"] is True and out["still_missing"] == [] and out["price"] == 55.0
        assert out["message"] == "Repaired A: price, rsi, pe_ratio, roce, sentiment_score — complete"
