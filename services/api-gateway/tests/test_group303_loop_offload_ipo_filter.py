"""tests/test_group303_loop_offload_ipo_filter.py

group303 (2026-10-10), from the boot log:

1. `event loop blocked for 4.0s` - an Oracle commit inside `store.put_symbol()` ran ON the event loop during repair-all.
   The feed-store reads/writes in async paths now go through `asyncio.to_thread`.
2. The IPO scanner sent hyphen-less NCD / zero-coupon / InvIT / series tickers (STFNCD8, UGROD1, ...) to Yahoo, and fell
   back to its own direct yfinance call even when market-data had already answered 404.

Run from services/api-gateway:
    python3 -m pytest tests/test_group303_loop_offload_ipo_filter.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import ipo_scanner as ipo
import symbol_aliases


# ── 1. feed-store calls leave the event loop ─────────────────────────────────

class ThreadRecordingStore:
    def __init__(self):
        self.get_threads, self.put_threads, self.puts = [], [], []

    def get_symbol(self, sym):
        self.get_threads.append(threading.get_ident())
        return {"symbol": sym, "price": 100.0, "rsi": 55.0, "pe_ratio": 20.0, "roce": 12.0,
                "sentiment_score": 0.5, "fundamental_score": 50, "sector": "X"}

    def put_symbol(self, sym, row, ttl=None):
        self.put_threads.append(threading.get_ident())
        self.puts.append((sym, ttl))


def test_repair_put_and_get_run_off_the_event_loop(monkeypatch):
    store = ThreadRecordingStore()
    monkeypatch.setattr(gw, "_feed_store", lambda: store)
    monkeypatch.setattr(symbol_aliases, "is_known_high_price", lambda s: False)
    monkeypatch.setattr(symbol_aliases, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(symbol_aliases, "is_learned_delisted", lambda s: False)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)

    loop_thread = {}

    async def main():
        loop_thread["id"] = threading.get_ident()
        return await gw._patch_single_stock_feed("ABC", client=types.SimpleNamespace())

    out = asyncio.run(main())
    assert out["symbol"] == "ABC"
    assert store.get_threads and store.put_threads, "store was never touched"
    assert loop_thread["id"] not in store.get_threads
    assert loop_thread["id"] not in store.put_threads
    assert store.puts == [("ABC", gw.DATA_FEED_TTL)]          # ttl still reaches the store


def test_no_direct_store_calls_remain_in_async_functions():
    """AST guard: any `<x>.put_symbol(...)` / `<x>.get_symbol(...)` call inside an `async def` of main.py must be an
    argument of asyncio.to_thread (i.e. referenced, not called)."""
    import ast
    src = open(gw.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, n):
            self.stack.append(False); self.generic_visit(n); self.stack.pop()

        def visit_AsyncFunctionDef(self, n):
            self.stack.append(True); self.generic_visit(n); self.stack.pop()

        def visit_Call(self, n):
            f = n.func
            if (self.stack and self.stack[-1] and isinstance(f, ast.Attribute)
                    and f.attr in ("put_symbol", "get_symbol")):
                offenders.append((n.lineno, f.attr))
            self.generic_visit(n)

    V().visit(tree)
    assert offenders == [], f"blocking feed-store calls on the event loop: {offenders}"


# ── 2. IPO scanner: non-equity filter ────────────────────────────────────────

NON_EQUITY_FROM_LOG = (
    "ADANIENPP1 CUBEINVIT EHFLNCD IHFL13 NMC01 PFCZCB1 SMC01 SMCG01 SMCG02 SMCG03 SNCD9T2 STFNCD8 TCFNCD2 "
    "UGROD1 UGROD2 UGROD3"
).split()
REAL_EQUITY_LOOKALIKES = "NSDL VALUE360 PENTAGOLD RKFAL EVENTIONS TCS RELIANCE NMDC SMCGLOBAL UGROCAP BAJAJHFL APPLEPP".split()


@pytest.mark.parametrize("sym", NON_EQUITY_FROM_LOG)
def test_debt_and_unit_tickers_are_non_equity(sym):
    assert ipo._is_ipo_non_equity(sym) is True


@pytest.mark.parametrize("sym", REAL_EQUITY_LOOKALIKES)
def test_real_equities_are_not_flagged(sym):
    assert ipo._is_ipo_non_equity(sym) is False


def test_series_prefixes_extend_from_env(monkeypatch):
    monkeypatch.setenv("IPO_NON_EQUITY_SERIES_PREFIXES", "ZZDEBT, qq")
    import importlib
    m = importlib.reload(ipo)
    try:
        assert m._is_ipo_non_equity("ZZDEBT7") and m._is_ipo_non_equity("QQ12")
        assert not m._is_ipo_non_equity("ZZDEBTX")
    finally:
        monkeypatch.delenv("IPO_NON_EQUITY_SERIES_PREFIXES", raising=False)
        importlib.reload(ipo)


# ── 3. IPO scanner: 404 is authoritative + negative cache ───────────────────

class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


@pytest.fixture
def history_env(monkeypatch):
    ipo._HISTORY_MISS.clear()
    monkeypatch.delenv("IPO_HISTORY_MISS_TTL_S", raising=False)
    e = types.SimpleNamespace(md_calls=[], yf_calls=[], status=404)

    def fake_get(url, **kw):
        e.md_calls.append(url)
        if isinstance(e.status, Exception):
            raise e.status
        return Resp(e.status, {"candles": []})

    class FakeYF(types.ModuleType):
        pass

    yf = FakeYF("yfinance")

    class Ticker:
        def __init__(self, t):
            e.yf_calls.append(t)

        def history(self, **kw):
            import pandas as pd
            return pd.DataFrame()

    yf.Ticker = Ticker
    al = types.ModuleType("symbol_aliases")
    al.resolve_ns_ticker = lambda s: s + ".NS"
    al.is_non_equity_instrument = lambda s: False
    monkeypatch.setattr(ipo.httpx, "get", fake_get)
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    monkeypatch.setitem(sys.modules, "symbol_aliases", al)
    yield e
    ipo._HISTORY_MISS.clear()


def test_404_from_market_data_skips_direct_yfinance(history_env):
    history_env.status = 404
    assert ipo._fetch_history("EVENTIONS", 4) is None
    assert history_env.yf_calls == []                 # the gateway no longer asks Yahoo a second time
    assert "EVENTIONS" in ipo._HISTORY_MISS


def test_known_missing_symbol_is_not_asked_again(history_env):
    history_env.status = 404
    ipo._fetch_history("EVENTIONS", 4)
    ipo._fetch_history("EVENTIONS", 4)
    ipo._fetch_history("eventions.ns", 4)             # same key after normalising
    assert len(history_env.md_calls) == 1


def test_miss_expires_after_ttl(history_env, monkeypatch):
    history_env.status = 404
    monkeypatch.setenv("IPO_HISTORY_MISS_TTL_S", "100")
    ipo._fetch_history("EVENTIONS", 4)
    ipo._HISTORY_MISS["EVENTIONS"] -= 101
    ipo._fetch_history("EVENTIONS", 4)
    assert len(history_env.md_calls) == 2


def test_ttl_zero_disables_the_cache(history_env, monkeypatch):
    history_env.status = 404
    monkeypatch.setenv("IPO_HISTORY_MISS_TTL_S", "0")
    ipo._fetch_history("EVENTIONS", 4)
    ipo._fetch_history("EVENTIONS", 4)
    assert len(history_env.md_calls) == 2 and ipo._HISTORY_MISS == {}


def test_unreachable_market_data_still_falls_back_to_yfinance(history_env):
    history_env.status = RuntimeError("md down")
    assert ipo._fetch_history("ABC", 4) is None       # fallback ran (and found nothing) ...
    assert history_env.yf_calls == ["ABC.NS"]         # ... so the direct call really happened
    assert "ABC" in ipo._HISTORY_MISS                 # an empty Yahoo answer is remembered too


def test_5xx_goes_to_fallback_and_is_not_marked_before_it_ran(history_env):
    history_env.status = 503
    ipo._fetch_history("ABC", 4)
    assert history_env.yf_calls == ["ABC.NS"]         # 503 = market-data unhealthy -> direct fallback is allowed


def test_miss_cache_is_bounded(history_env):
    for i in range(ipo._HISTORY_MISS_MAX + 50):
        ipo._history_mark_missing(f"S{i}")
    assert len(ipo._HISTORY_MISS) <= ipo._HISTORY_MISS_MAX


def test_blank_ttl_env_uses_the_default(monkeypatch):
    monkeypatch.setenv("IPO_HISTORY_MISS_TTL_S", "")
    assert ipo._history_miss_ttl_s() == 10800.0
    monkeypatch.setenv("IPO_HISTORY_MISS_TTL_S", "garbage")
    assert ipo._history_miss_ttl_s() == 10800.0
