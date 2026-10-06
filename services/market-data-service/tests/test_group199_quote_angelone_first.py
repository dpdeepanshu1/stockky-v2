"""
group199: GET /quote prices symbols that are outside the live feed from the AngelOne REST quote first.

On the 2026-10-06 VM STEAMHOUSE / SGRL / KENNAMET / ROSSTECH (real NSE -EQ stocks, not in the 489-symbol live feed)
took 5-20 s on /quote because Yahoo was the first source asked, while ELEVATE (in the feed) answered in 0 ms.
real-trade-service's short read timeout made them look "never priced". Any AngelOne miss must fall through to the
unchanged Yahoo path.

Run from services/market-data-service:
    python3 -m pytest tests/test_group199_quote_angelone_first.py -v
"""
from __future__ import annotations
import asyncio, os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("QUOTE_ANGELONE_FIRST", "QUOTE_ANGELONE_FIRST_TIMEOUT_S"):
    os.environ.pop(_k, None)

import pytest

import main
import angelone_client
import angelone_scrip_master


class FakeSession:
    def __init__(self, answer=None, configured=True, exc=None, delay=0.0):
        self.answer, self.configured, self.exc, self.delay = answer, configured, exc, delay
        self.calls = []

    def is_configured(self):
        return self.configured

    async def get_quote(self, exchange, token, max_wait=20.0):
        self.calls.append((exchange, token, max_wait))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.answer


GOOD = {"ltp": 715.0, "close": 694.25, "high": 720.0, "low": 700.5, "tradeVolume": 12345, "symbolToken": "764885"}


@pytest.fixture()
def ao(monkeypatch):
    sess = FakeSession(dict(GOOD))
    monkeypatch.setattr(angelone_client, "get_session", lambda: sess)
    monkeypatch.setattr(angelone_scrip_master, "get_token", lambda base: "764885")
    import rate_limiter
    monkeypatch.setattr(rate_limiter, "in_cooldown", lambda p: False)
    monkeypatch.setattr(main, "_waterfall_equity_base", lambda s: str(s).upper().replace(".NS", ""))
    monkeypatch.delenv("QUOTE_ANGELONE_FIRST", raising=False)
    monkeypatch.delenv("QUOTE_ANGELONE_FIRST_TIMEOUT_S", raising=False)
    return sess


class TestHelper:
    def test_builds_quote_shape(self, ao):
        r = main._angelone_rest_quote_first("SGRL.NS")
        assert r["price"] == 715.0 and r["cmp"] == 715.0 and r["source"] == "angelone_rest"
        assert r["previous_close"] == 694.25 and r["day_change_pct"] == round((715 - 694.25) / 694.25 * 100, 2)
        assert r["day_high"] == 720.0 and r["day_low"] == 700.5 and r["volume"] == 12345
        assert r["symbol"] == "SGRL"
        assert ao.calls == [("NSE", "764885", 2.0)]

    def test_missing_optional_fields_stay_none(self, ao):
        ao.answer = {"ltp": 50.0}
        r = main._angelone_rest_quote_first("ABC")
        assert r["price"] == 50.0
        assert r["previous_close"] is None and r["day_change_pct"] is None
        assert r["day_high"] is None and r["day_low"] is None and r["volume"] is None

    def test_off_switch(self, ao, monkeypatch):
        for off in ("0", "false", "off", "no"):
            monkeypatch.setenv("QUOTE_ANGELONE_FIRST", off)
            assert main._angelone_rest_quote_first("SGRL") is None
        assert ao.calls == []

    @pytest.mark.parametrize("answer", [None, {}, {"ltp": 0}, {"ltp": -3}, {"ltp": "x"}, {"close": 10}, "oops"])
    def test_bad_answers_are_a_miss(self, ao, answer):
        ao.answer = answer
        assert main._angelone_rest_quote_first("SGRL") is None

    def test_not_configured(self, ao):
        ao.configured = False
        assert main._angelone_rest_quote_first("SGRL") is None
        assert ao.calls == []

    def test_cooldown(self, ao, monkeypatch):
        import rate_limiter
        monkeypatch.setattr(rate_limiter, "in_cooldown", lambda p: p == "angelone_quote")
        assert main._angelone_rest_quote_first("SGRL") is None
        assert ao.calls == []

    def test_no_token_and_no_base(self, ao, monkeypatch):
        monkeypatch.setattr(angelone_scrip_master, "get_token", lambda b: None)
        assert main._angelone_rest_quote_first("SGRL") is None
        monkeypatch.setattr(angelone_scrip_master, "get_token", lambda b: "1")
        monkeypatch.setattr(main, "_waterfall_equity_base", lambda s: "")
        assert main._angelone_rest_quote_first("SGRL") is None
        assert ao.calls == []

    @pytest.mark.parametrize("sym", ["^NSEI", "NIFTYBEES", "nifty50"])
    def test_indices_skipped(self, ao, sym):
        assert main._angelone_rest_quote_first(sym) is None
        assert ao.calls == []

    def test_exception_is_a_miss(self, ao):
        ao.exc = RuntimeError("boom")
        assert main._angelone_rest_quote_first("SGRL") is None

    def test_slow_call_is_cut_off(self, ao, monkeypatch):
        monkeypatch.setenv("QUOTE_ANGELONE_FIRST_TIMEOUT_S", "0.5")
        ao.delay = 5.0
        import time
        t0 = time.monotonic()
        assert main._angelone_rest_quote_first("SGRL") is None
        assert time.monotonic() - t0 < 2.5

    def test_inside_running_loop_is_a_miss(self, ao):
        async def run():
            return main._angelone_rest_quote_first("SGRL")
        assert asyncio.run(run()) is None

    def test_timeout_config_bounds(self, monkeypatch):
        for raw, want in (("", 6.0), ("abc", 6.0), ("0.1", 6.0), ("99", 6.0), ("3", 3.0)):
            monkeypatch.setenv("QUOTE_ANGELONE_FIRST_TIMEOUT_S", raw)
            assert main._quote_angelone_first_cfg() == (True, want)


@pytest.fixture()
def route(monkeypatch, ao):
    cache = {}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_get", lambda k: None)
    monkeypatch.setattr(main, "_fallback_set", lambda k, v: None)
    monkeypatch.setattr(main, "normalize_symbol", lambda s: str(s).upper().replace(".NS", ""))
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    for name in ("angelone_ws_feed", "yahoo_ws_feed"):
        m = types.ModuleType(name)
        m.get_live_quote = lambda s: None
        monkeypatch.setitem(sys.modules, name, m)
    main._neg_reset()
    return cache


class TestRoute:
    def test_angelone_answers_and_yahoo_is_never_called(self, route, monkeypatch):
        def boom(sym):
            raise AssertionError("Yahoo must not be asked when AngelOne priced the symbol")
        monkeypatch.setattr(main, "_yahoo_ohlcv_quote", boom)
        r = main.get_quote("SGRL")
        assert r["price"] == 715.0 and r["source"] == "angelone_rest"
        assert route["quote:SGRL"]["source"] == "angelone_rest"

    def test_miss_falls_through_to_yahoo(self, route, ao, monkeypatch):
        ao.answer = {}
        monkeypatch.setattr(main, "_yahoo_ohlcv_quote",
                            lambda sym: {"symbol": sym, "price": 118.4, "cmp": 118.4, "source": "yahoo",
                                         "fetched_at": "2026-10-06T08:36:35"})
        r = main.get_quote("STEAMHOUSE")
        assert r["price"] == 118.4 and r["source"] == "yahoo"

    def test_off_switch_goes_straight_to_yahoo(self, route, ao, monkeypatch):
        monkeypatch.setenv("QUOTE_ANGELONE_FIRST", "0")
        monkeypatch.setattr(main, "_yahoo_ohlcv_quote",
                            lambda sym: {"symbol": sym, "price": 4252.0, "cmp": 4252.0, "source": "yahoo",
                                         "fetched_at": "2026-10-06T08:37:16"})
        r = main.get_quote("KENNAMET")
        assert r["source"] == "yahoo" and ao.calls == []

    def test_fresh_cache_still_wins_without_a_call(self, route, ao):
        route["quote:ROSSTECH"] = {"symbol": "ROSSTECH", "price": 1624.3, "source": "yahoo"}
        monkey_calls = len(ao.calls)
        r = main._get_quote_inner("ROSSTECH")
        assert r["price"] == 1624.3 and len(ao.calls) == monkey_calls

    def test_a_priced_answer_clears_the_negative_cache(self, route):
        main._NEG_QUOTE["SGRL"] = [1, 0.0]
        main.get_quote("SGRL")
        assert "SGRL" not in main._NEG_QUOTE


class TestClientFailClosed:
    def _client(self, monkeypatch):
        sess = angelone_client.AngelOneSession()
        async def noop(): return None
        monkeypatch.setattr(sess, "ensure_session", noop)
        monkeypatch.setattr(angelone_client, "_rl_in_cooldown", lambda p: False)
        return sess

    def test_busy_bucket_returns_empty_without_http(self, monkeypatch):
        sess = self._client(monkeypatch)
        seen = {}
        monkeypatch.setattr(angelone_client, "_rl_try_acquire", lambda p, weight=1, max_wait=5.0: seen.setdefault("w", max_wait) and False)
        class NoHttp:
            def __init__(self, *a, **k): raise AssertionError("no request may be sent")
        monkeypatch.setattr(angelone_client.httpx, "AsyncClient", NoHttp)
        assert asyncio.run(sess.get_quote("NSE", "1", max_wait=2.0)) == {}
        assert seen["w"] == 2.0

    def test_default_keeps_the_blocking_acquire(self, monkeypatch):
        sess = self._client(monkeypatch)
        used = {"acquire": 0, "try": 0}
        monkeypatch.setattr(angelone_client, "_rl_acquire", lambda p, weight=1, max_wait=20.0: used.__setitem__("acquire", used["acquire"] + 1) or 0.0)
        monkeypatch.setattr(angelone_client, "_rl_try_acquire", lambda *a, **k: used.__setitem__("try", used["try"] + 1) or True)
        class Boom:
            def __init__(self, *a, **k): raise RuntimeError("stop after the limiter")
        monkeypatch.setattr(angelone_client.httpx, "AsyncClient", Boom)
        with pytest.raises(RuntimeError):
            asyncio.run(sess.get_quote("NSE", "1"))
        assert used == {"acquire": 1, "try": 0}
