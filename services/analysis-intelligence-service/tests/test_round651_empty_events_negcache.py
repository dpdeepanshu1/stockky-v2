"""Round 651: a symbol with nothing found and no older answer is remembered for EVENT_EMPTY_NEGCACHE_S (default 300 s), so one name is not fetched in
full on every pass (19 Nov log: GHCLTEXTIL 6x, BLUECLOUDS 4x, SHRINGARMS 3x in 15 min, all sources "0 items").  It is never written to the fallback key.

Plain runner: `python tests/test_round651_empty_events_negcache.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import types
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))


def _stub_missing(name, **attrs):
    try:
        __import__(name)
    except Exception:  # noqa: BLE001 - only a missing optional package is stubbed (the VM image has them)
        m = mock.MagicMock(name=name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m


_stub_missing("yfinance")
_stub_missing("feedparser")
_stub_missing("httpx")
_stub_missing("upstash_redis")
_stub_missing("pydantic", BaseModel=type("BaseModel", (), {}))
_stub_missing("fastapi")
_stub_missing("fastapi.middleware")
_stub_missing("fastapi.middleware.cors")

import main as em  # noqa: E402  (event/main.py)


def _quiet(monkeypatch=None, news=None):
    """Every yfinance getter answers nothing; news comes from `news`."""
    patches = []
    for name in ("_get_earnings_dates", "_get_dividends", "_get_splits", "_get_insider_transactions", "_get_upgrades_downgrades",
                 "_get_institutional_holders", "_get_earnings_history"):
        patches.append(mock.patch.object(em, name, return_value=None))
    calls = {"n": 0}

    def fake_news(sym, max_total=15):
        calls["n"] += 1
        return list(news or [])
    patches.append(mock.patch.object(em, "_fetch_news_from_multiple_sources", side_effect=fake_news))
    return patches, calls


def _run(fn, news=None, ttl_env=None):
    patches, calls = _quiet(news=news)
    env = {"EVENT_EMPTY_NEGCACHE_S": ttl_env} if ttl_env is not None else {}
    em._mem.clear()
    em._mem_exp.clear()
    for p in patches:
        p.start()
    try:
        with mock.patch.dict(os.environ, env):
            return fn(calls)
    finally:
        for p in patches:
            p.stop()


def test_empty_symbol_is_fetched_once_within_the_window():
    def go(calls):
        em._fetch_events_impl("GHCLTEXTIL")
        r2 = em._fetch_events_impl("GHCLTEXTIL")
        return calls["n"], r2
    n, r2 = _run(go)
    assert n == 1                       # the second call was a cache hit
    assert r2.get("empty_negcache") is True and not r2.get("recent_news")


def test_negative_entry_expires_and_is_fetched_again():
    def go(calls):
        em._fetch_events_impl("GHCLTEXTIL")
        em._mem_exp[f"{em.EVENT_CACHE_PREFIX}GHCLTEXTIL.NS"] = 0   # expired
        em._fetch_events_impl("GHCLTEXTIL")
        return calls["n"]
    assert _run(go) == 2


def test_setting_zero_turns_it_off():
    def go(calls):
        em._fetch_events_impl("GHCLTEXTIL")
        em._fetch_events_impl("GHCLTEXTIL")
        return calls["n"]
    assert _run(go, ttl_env="0") == 2


def test_force_bypasses_the_negative_entry():
    def go(calls):
        em._fetch_events_impl("GHCLTEXTIL")
        em._fetch_events_impl("GHCLTEXTIL", force=True)
        return calls["n"]
    assert _run(go) == 2


def test_negative_entry_never_touches_the_fallback_key():
    def go(calls):
        em._fetch_events_impl("GHCLTEXTIL")
        return em._redis_get(f"{em.EVENT_FALLBACK_PREFIX}GHCLTEXTIL.NS")
    assert _run(go) is None


def test_symbol_with_news_keeps_its_normal_ttl_and_no_negative_flag():
    news = [{"title": "GHCL Textiles wins order", "published": "2026-11-19T04:00:00+00:00", "url": "u", "publisher": "p"}]

    def go(calls):
        r = em._fetch_events_impl("GHCLTEXTIL")
        return r, em._redis_get(f"{em.EVENT_CACHE_PREFIX}GHCLTEXTIL.NS")
    r, cached = _run(go, news=news)
    assert r.get("empty_negcache") is None and cached is not None and cached.get("empty_negcache") is None


def test_stale_fallback_branch_is_unchanged():
    def go(calls):
        em._redis_set(f"{em.EVENT_FALLBACK_PREFIX}GHCLTEXTIL.NS",
                      {"symbol": "GHCLTEXTIL.NS", "recent_news": [], "next_earnings_date": "2026-12-01"}, ttl=1000)
        r = em._fetch_events_impl("GHCLTEXTIL")
        return r
    # next_earnings_date in the fallback only; the live fetch is empty, so the fallback is served (round 600 behaviour)
    r = _run(go)
    assert r.get("stale") is True and "empty_negcache" not in r


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
