"""tests/test_main_hot_stocks.py — coverage for api-gateway/main.py, slice 10 (lines 6811-7380)

Pass 68. The whole Stockky 🔥 Hot Stocks section:

* the TTL helpers (`_seconds_until_next_market_open`, `_hot_stocks_ttl`) and `_hot_payload_fingerprint`;
* `_warm_upstream_services` and the two never-fatal Hot Picks store shims
  (`hotpicks_stop_requested`, `hotpicks_db_upsert`);
* `_build_hot_conviction_extra`;
* `stockky_hot_stocks` — cache hit / `force`, the catalyst-first universe (source caps, de-duplication,
  failing sources, nifty fallback, `max_symbols`), the last-scan seed, batching + warm, `progress_cb`
  (with and without the `batch` kwarg), the Stop button (partial payload + shortened TTL), the per-symbol
  evaluation (news / results / bulk-insider sections, catalyst promotion, weak-news filter), the two-pass
  price enrichment, ranking + per-section caps, the payload shape and the durable upsert.

Everything downstream is faked: the universe sources, the cached news / event fetchers, the kv layer, the
feed store, the bulk price fetch, the warm-up, the Hot Picks store and `asyncio.sleep` (so batch pauses
are instant). Nothing touches the network or a database. Findings are pinned as current behaviour and
marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_hot_stocks.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import hotpicks_store


def _run(coro):
    return asyncio.run(coro)


# ═════════════════════════════════════════════════════════════════════════════
# _seconds_until_next_market_open / _hot_stocks_ttl / _hot_payload_fingerprint
# ═════════════════════════════════════════════════════════════════════════════

def _freeze_now(monkeypatch, year, month, day, hour, minute=0, holidays=()):
    fixed = datetime(year, month, day, hour, minute, tzinfo=gw.IST)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    hol = {datetime(*h).date() for h in holidays}
    monkeypatch.setattr(gw, "datetime", _DT)
    monkeypatch.setattr(gw, "is_nse_holiday", lambda d: d in hol)
    return fixed


# 2026-09-30 is a Wednesday, 2026-10-02 a Friday, 2026-10-03 a Saturday.
class TestSecondsUntilNextOpen:
    def test_before_the_open_waits_for_today(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 9, 30, 8, 0)
        assert gw._seconds_until_next_market_open() == 75 * 60          # 08:00 -> 09:15

    def test_inside_the_final_hour_is_floored_to_one_hour(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 9, 30, 9, 0)                      # 15 min to go
        assert gw._seconds_until_next_market_open() == 3600

    def test_exactly_at_the_open_rolls_to_the_next_day(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 9, 30, 9, 15)
        assert gw._seconds_until_next_market_open() == 24 * 3600

    def test_after_the_open_waits_for_tomorrow(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 9, 30, 11, 0)
        assert gw._seconds_until_next_market_open() == 22 * 3600 + 15 * 60

    def test_friday_after_the_open_skips_the_weekend(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 10, 2, 11, 0)
        assert gw._seconds_until_next_market_open() == 70 * 3600 + 15 * 60     # -> Monday 09:15

    def test_holiday_is_skipped(self, monkeypatch):
        _freeze_now(monkeypatch, 2026, 9, 30, 11, 0, holidays=[(2026, 10, 1)])  # Thursday closed
        assert gw._seconds_until_next_market_open() == 46 * 3600 + 15 * 60      # -> Friday 09:15

    def test_endless_closures_stop_after_the_14_day_lookahead(self, monkeypatch):
        fixed = _freeze_now(monkeypatch, 2026, 9, 30, 11, 0)
        monkeypatch.setattr(gw, "is_nse_holiday", lambda d: True)
        first = fixed.replace(hour=9, minute=15) + timedelta(days=1)
        expected = int((first + timedelta(days=14) - fixed).total_seconds())
        assert gw._seconds_until_next_market_open() == expected


class TestHotStocksTtl:
    @pytest.mark.parametrize("phase", ["preopen", "open", "post"])
    def test_live_phases_use_the_default_open_ttl(self, monkeypatch, phase):
        monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: phase)
        monkeypatch.setattr(gw, "HOT_STOCKS_TTL_OPEN_DEFAULT", 180)
        assert gw._hot_stocks_ttl() == 180

    @pytest.mark.parametrize("default,expected", [(10, 120), (9999, 300), (120, 120), (300, 300)])
    def test_open_ttl_is_clamped_to_the_configured_band(self, monkeypatch, default, expected):
        monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "open")
        monkeypatch.setattr(gw, "HOT_STOCKS_TTL_OPEN_DEFAULT", default)
        assert gw._hot_stocks_ttl() == expected

    @pytest.mark.parametrize("phase", ["closed", "holiday"])
    def test_off_hours_cache_until_the_next_open(self, monkeypatch, phase):
        monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: phase)
        monkeypatch.setattr(gw, "_seconds_until_next_market_open", lambda: 12345)
        assert gw._hot_stocks_ttl() == 12345


class TestFingerprint:
    def test_fingerprint_covers_the_three_sections_in_order(self):
        payload = {
            "news_driven": [{"symbol": "AAA", "decision": "BUY NOW", "score": 70, "headline_count": 3}],
            "results_driven": [{"symbol": "BBB", "decision": "HOLD", "score": 50, "next_earnings_date": "2026-10-20"}],
            "bulk_insider_driven": [{"symbol": "CCC"}],
        }
        assert gw._hot_payload_fingerprint(payload) == (
            "AAA:BUY NOW:70:3:None|BBB:HOLD:50:None:2026-10-20|CCC:None:None:None:None")

    def test_missing_or_none_sections_are_skipped(self):
        assert gw._hot_payload_fingerprint({}) == ""
        assert gw._hot_payload_fingerprint({"news_driven": None, "results_driven": None}) == ""

    def test_fingerprint_is_cut_at_800_chars(self):
        payload = {"news_driven": [{"symbol": f"SYM{i:04d}", "decision": "BUY NOW", "score": 99}
                                   for i in range(200)]}
        assert len(gw._hot_payload_fingerprint(payload)) == 800

    def test_fingerprint_ignores_fields_outside_the_key_set(self):
        a = {"news_driven": [{"symbol": "A", "decision": "X", "score": 1, "summary": "one"}]}
        b = {"news_driven": [{"symbol": "A", "decision": "X", "score": 1, "summary": "two"}]}
        assert gw._hot_payload_fingerprint(a) == gw._hot_payload_fingerprint(b)

    @pytest.mark.parametrize("bad", [None, "text", {"news_driven": ["not-a-dict"]}])
    def test_malformed_payloads_give_an_empty_fingerprint(self, bad):
        assert gw._hot_payload_fingerprint(bad) == ""


# ═════════════════════════════════════════════════════════════════════════════
# _warm_upstream_services
# ═════════════════════════════════════════════════════════════════════════════

class WarmClient:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)          # full URLs that raise

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if url in self.fail:
            raise RuntimeError("down")
        return object()


def _set_service_urls(monkeypatch, **urls):
    for name in ("FUNDAMENTAL_URL", "EVENT_URL", "NEWS_URL", "TECHNICAL_URL", "MARKET_DATA_URL", "NOTIFICATION_URL"):
        monkeypatch.setattr(gw, name, urls.get(name, ""), raising=False)
        monkeypatch.delenv(name, raising=False)


class TestWarmUpstreamServices:
    def test_each_service_is_pinged_with_warm_health_first_and_only_once_when_it_answers(self, monkeypatch):
        _set_service_urls(monkeypatch, FUNDAMENTAL_URL="http://fund.local/", NEWS_URL="http://news.local")
        c = WarmClient()
        _run(gw._warm_upstream_services(c))
        assert c.calls == [("http://fund.local/health?warm=true", 6.0), ("http://news.local/health?warm=true", 6.0)]

    def test_falls_back_to_plain_health_and_survives_total_failure(self, monkeypatch):
        _set_service_urls(monkeypatch, FUNDAMENTAL_URL="http://fund.local", NEWS_URL="http://news.local")
        c = WarmClient(fail={"http://fund.local/health?warm=true",
                             "http://news.local/health?warm=true", "http://news.local/health"})
        _run(gw._warm_upstream_services(c))
        urls = [u for u, _ in c.calls]
        assert urls == ["http://fund.local/health?warm=true", "http://fund.local/health",
                        "http://news.local/health?warm=true", "http://news.local/health"]

    def test_env_var_is_used_when_the_module_global_is_empty(self, monkeypatch):
        _set_service_urls(monkeypatch)
        monkeypatch.setenv("EVENT_URL", "http://evt.env/")
        c = WarmClient()
        _run(gw._warm_upstream_services(c))
        assert c.calls == [("http://evt.env/health?warm=true", 6.0)]

    def test_no_configured_services_means_no_calls(self, monkeypatch):
        _set_service_urls(monkeypatch)
        c = WarmClient()
        _run(gw._warm_upstream_services(c))
        assert c.calls == []

    def test_shared_client_is_used_when_none_is_passed(self, monkeypatch):
        _set_service_urls(monkeypatch, TECHNICAL_URL="http://tech.local")
        c = WarmClient()
        monkeypatch.setattr(gw, "_get_http_client", lambda: c)
        _run(gw._warm_upstream_services())
        assert c.calls == [("http://tech.local/health?warm=true", 6.0)]


# ═════════════════════════════════════════════════════════════════════════════
# Hot Picks store shims
# ═════════════════════════════════════════════════════════════════════════════

class TestHotPicksShims:
    @pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False), (1, True), ("", False)])
    def test_stop_requested_coerces_to_bool(self, monkeypatch, value, expected):
        monkeypatch.setattr(hotpicks_store, "hotpicks_stop_requested", lambda: value)
        assert gw.hotpicks_stop_requested() is expected

    def test_stop_requested_failure_means_not_requested(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(hotpicks_store, "hotpicks_stop_requested", boom)
        assert gw.hotpicks_stop_requested() is False

    def test_stop_requested_without_the_store_module_means_not_requested(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hotpicks_store", None)
        assert gw.hotpicks_stop_requested() is False

    @pytest.mark.parametrize("value,expected", [(3, 3), ("4", 4), (None, 0), (0, 0)])
    def test_db_upsert_returns_an_int(self, monkeypatch, value, expected):
        monkeypatch.setattr(hotpicks_store, "hotpicks_db_upsert", lambda p: value)
        assert gw.hotpicks_db_upsert({"x": 1}) == expected

    def test_db_upsert_passes_the_payload_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hotpicks_store, "hotpicks_db_upsert", lambda p: seen.append(p) or 2)
        payload = {"news_driven": []}
        assert gw.hotpicks_db_upsert(payload) == 2 and seen == [payload]

    def test_db_upsert_failure_means_zero(self, monkeypatch):
        def boom(p):
            raise RuntimeError("db down")

        monkeypatch.setattr(hotpicks_store, "hotpicks_db_upsert", boom)
        assert gw.hotpicks_db_upsert({}) == 0

    def test_db_upsert_without_the_store_module_means_zero(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hotpicks_store", None)
        assert gw.hotpicks_db_upsert({}) == 0


# ═════════════════════════════════════════════════════════════════════════════
# _build_hot_conviction_extra
# ═════════════════════════════════════════════════════════════════════════════

class TestConvictionExtra:
    def test_no_sources_gives_nothing(self):
        assert gw._build_hot_conviction_extra(None, None) == {}
        assert gw._build_hot_conviction_extra("junk", ["junk"]) == {}

    def test_the_fuller_decide_record_wins_over_the_light_seed(self):
        seed = {"combined_score": 50, "technical_score": 40, "target": 100.0}
        full = {"combined_score": 80, "fundamental_score": 70}
        out = gw._build_hot_conviction_extra(full, seed)
        assert out["combined_score"] == 80 and out["technical_score"] == 40
        assert out["fundamental_score"] == 70 and out["target"] == 100.0

    def test_none_and_na_never_clobber_a_real_value(self):
        seed = {"combined_score": 50, "holding_period": "2 weeks"}
        full = {"combined_score": None, "holding_period": "N/A", "news_score": 0}
        out = gw._build_hot_conviction_extra(full, seed)
        assert out["combined_score"] == 50 and out["holding_period"] == "2 weeks"
        assert out["news_score"] == 0                      # a real zero is kept

    def test_unrelated_keys_are_not_copied(self):
        assert gw._build_hot_conviction_extra({"symbol": "AAA", "reasons": ["x"], "decision": "BUY NOW"}, None) == {}

    def test_horizon_is_derived_from_entry_and_target_when_missing(self):
        out = gw._build_hot_conviction_extra({"entry_range": {"low": 100.0, "high": 102.0}, "target": 110.0}, None)
        assert isinstance(out["holding_period_estimate"], dict)
        assert out["holding_period_estimate"]["min_days"] > 0

    def test_entry_high_is_used_when_low_is_missing_or_zero(self):
        a = gw._build_hot_conviction_extra({"entry_range": {"high": 100.0}, "target": 110.0}, None)
        b = gw._build_hot_conviction_extra({"entry_range": {"low": 0, "high": 100.0}, "target": 110.0}, None)
        assert a["holding_period_estimate"] == b["holding_period_estimate"]

    def test_an_explicit_estimate_is_not_overwritten(self):
        out = gw._build_hot_conviction_extra(
            {"entry_range": {"low": 100.0}, "target": 110.0, "holding_period_estimate": {"label": "mine"}}, None)
        assert out["holding_period_estimate"] == {"label": "mine"}

    @pytest.mark.parametrize("record", [
        {"target": 110.0},                                              # no entry
        {"entry_range": {"low": 100.0}},                                # no target
        {"entry_range": "100-102", "target": 110.0},                    # entry not a dict
        {"entry_range": {}, "target": 110.0},                           # empty entry
        {"entry_range": {"low": "abc"}, "target": 110.0},               # unparsable -> swallowed
    ])
    def test_no_horizon_when_it_cannot_be_derived(self, record):
        assert "holding_period_estimate" not in gw._build_hot_conviction_extra(record, None)


# ═════════════════════════════════════════════════════════════════════════════
# stockky_hot_stocks
# ═════════════════════════════════════════════════════════════════════════════

class KV:
    def __init__(self):
        self.store = {}
        self.sets = []                    # (key, value, ttl)
        self.get_raises = set()           # keys whose read raises
        self.set_raises = set()           # key prefixes whose write raises

    def get(self, key):
        if key in self.get_raises:
            raise RuntimeError("kv read down")
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        if any(key.startswith(p) for p in self.set_raises):
            raise RuntimeError("kv write down")
        self.sets.append((key, value, ttl))
        self.store[key] = value


class HEnv:
    def __init__(self):
        self.momentum = []
        self.news_syms = []
        self.event_syms = []
        self.watch = []
        self.searched = []
        self.ipos = []
        self.nifty = []
        self.news = {}              # base -> dict | Exception | None
        self.events = {}
        self.news_calls = []
        self.source_calls = {"momentum": 0, "news": 0, "events": 0}
        self.rows = {}              # feed store rows: symbol -> {"px": float} | Exception
        self.store_raises = False
        self.resolver_raises = set()
        self.bulk_prices = {}
        self.bulk_raises = None
        self.bulk_calls = []
        self.stop_after = None      # int: the check right after this many symbols returns True
        self.stop_raises = False
        self.stop_checks = 0
        self.warm_calls = []
        self.warm_raises = False
        self.sleeps = []
        self.upsert = 0
        self.upsert_calls = []
        self.ttl = 180
        self.phase = "open"
        self.client = object()


class _SleepProxy:
    def __init__(self, sleeps):
        self._sleeps = sleeps

    async def sleep(self, secs):
        self._sleeps.append(secs)

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    return k


@pytest.fixture
def henv(monkeypatch, kv):
    env = HEnv()

    def listy(attr, counter=None):
        def fn():
            if counter:
                env.source_calls[counter] += 1
            v = getattr(env, attr)
            if isinstance(v, Exception):
                raise v
            return v
        return fn

    async def fetch_news(base, client):
        env.news_calls.append(base)
        v = env.news.get(base)
        if isinstance(v, Exception):
            raise v
        return v

    async def fetch_events(base, client):
        v = env.events.get(base)
        if isinstance(v, Exception):
            raise v
        return v

    class Store:
        def get_symbol(self, sym):
            v = env.rows.get(sym)
            if isinstance(v, Exception):
                raise v
            return v

    def feed_store():
        if env.store_raises:
            raise RuntimeError("store down")
        return Store()

    def resolved(row):
        if row.get("boom"):
            raise RuntimeError("resolver down")
        return float(row.get("px", 0.0))

    async def bulk(symbols, client):
        env.bulk_calls.append(sorted(symbols))
        if env.bulk_raises:
            raise env.bulk_raises
        return env.bulk_prices

    def stop():
        env.stop_checks += 1
        if env.stop_raises:
            raise RuntimeError("stop flag down")
        return env.stop_after is not None and env.stop_checks > env.stop_after

    async def warm(client):
        env.warm_calls.append(client)
        if env.warm_raises:
            raise RuntimeError("warm down")

    def upsert(payload):
        env.upsert_calls.append(payload)
        if isinstance(env.upsert, Exception):
            raise env.upsert
        return env.upsert

    monkeypatch.setattr(gw, "_load_watchlist", lambda: list(env.watch))
    monkeypatch.setattr(gw, "_load_searched", lambda: list(env.searched))
    monkeypatch.setattr(gw, "_get_recent_ipos", lambda: list(env.ipos))
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: env.nifty)
    monkeypatch.setattr(gw, "_get_momentum_movers", listy("momentum", "momentum"))
    monkeypatch.setattr(gw, "_get_news_mentioned_symbols", listy("news_syms", "news"))
    monkeypatch.setattr(gw, "_get_event_symbols", listy("event_syms", "events"))
    monkeypatch.setattr(gw, "_fetch_news_cached", fetch_news)
    monkeypatch.setattr(gw, "_fetch_events_cached", fetch_events)
    monkeypatch.setattr(gw, "_get_http_client", lambda: env.client)
    monkeypatch.setattr(gw, "_feed_store", feed_store)
    monkeypatch.setattr(gw, "_feed_resolved_price", resolved)
    monkeypatch.setattr(gw, "_fetch_prices_bulk_async", bulk)
    monkeypatch.setattr(gw, "hotpicks_stop_requested", stop)
    monkeypatch.setattr(gw, "hotpicks_db_upsert", upsert)
    monkeypatch.setattr(gw, "_warm_upstream_services", warm)
    monkeypatch.setattr(gw, "_hot_stocks_ttl", lambda: env.ttl)
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: env.phase)
    monkeypatch.setattr(gw, "asyncio", _SleepProxy(env.sleeps))
    for name in ("HOT_BATCH_SIZE", "CATALYST_BATCH_SIZE", "HOT_PARTIAL_CACHE_TTL"):
        monkeypatch.delenv(name, raising=False)
    return env


def _hot(**kw):
    return _run(gw.stockky_hot_stocks(**kw))


def _news(hc=0, score=None, summary="", headlines=None):
    return {"headline_count": hc, "news_score": score, "summary": summary,
            "headlines": headlines if headlines is not None else [f"h{i}" for i in range(hc)]}


def _results(**kw):
    d = {"next_earnings_date": "2026-10-20"}
    d.update(kw)
    return d


def _syms(rows):
    return [r["symbol"] for r in rows]


# ── cache ───────────────────────────────────────────────────────────────────

class TestHotCache:
    def test_cache_hit_is_served_without_scanning(self, henv, kv):
        kv.store[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [], "generated_at": "then", "cached": False}
        henv.watch = ["AAA"]
        out = _hot()
        assert out == {"news_driven": [], "generated_at": "then", "cached": True}
        assert henv.source_calls == {"momentum": 0, "news": 0, "events": 0} and henv.news_calls == []
        assert kv.sets == [] and henv.upsert_calls == []

    def test_force_ignores_the_cache(self, henv, kv):
        kv.store[gw.HOT_STOCKS_CACHE_KEY] = {"news_driven": [], "cached": False}
        out = _hot(force=True)
        assert out["cached"] is False and henv.source_calls["momentum"] == 1

    @pytest.mark.parametrize("cached", [None, {}])
    def test_empty_cache_values_trigger_a_scan(self, henv, kv, cached):
        if cached is not None:
            kv.store[gw.HOT_STOCKS_CACHE_KEY] = cached
        assert _hot()["cached"] is False and henv.source_calls["momentum"] == 1


# ── seed from the last full scan ────────────────────────────────────────────

class TestHotScanSeed:
    def test_actionable_rows_seed_the_universe_and_the_last_decision_cache(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {
            "recommendations": [
                {"symbol": "AAA.NS", "decision": "buy now", "combined_score": 77, "reasons": ["r1"],
                 "technical_score": 61, "entry_range": {"low": 1, "high": 2}, "target": None, "close": 5},
                {"symbol": "BBB.BO", "decision": "PREPARE TO BUY", "score": 64},
                {"symbol": "CCC", "decision": "HOLD", "combined_score": 99},        # not actionable
                {"symbol": "", "decision": "BUY NOW"},                              # no symbol
                "not-a-dict",
            ],
            "all_results": [{"symbol": "AAA", "decision": "BUY NOW", "combined_score": 77}],   # duplicate
        }
        out = _hot()
        assert out["universe_size"] == 2 and out["scan_seed_count"] == 2
        first_a = [v for k, v, _ in kv.sets if k == "stockky:last_decision:AAA"][0]
        assert first_a["decision"] == "buy now" and first_a["score"] == 77 and first_a["reasons"] == ["r1"]
        assert first_a["technical_score"] == 61 and first_a["entry_range"] == {"low": 1, "high": 2}
        assert "target" not in first_a and "close" not in first_a          # None / unlisted fields skipped
        # the duplicate row in all_results is written later and overwrites the richer first write
        assert kv.store["stockky:last_decision:AAA"]["decision"] == "BUY NOW"
        assert "technical_score" not in kv.store["stockky:last_decision:AAA"]
        assert (("stockky:last_decision:BBB", {"decision": "PREPARE TO BUY", "score": 64, "reasons": []}, 86400)
                in kv.sets)
        assert "stockky:last_decision:CCC" not in kv.store

    def test_lowercase_exchange_suffix_is_stripped_from_scan_seeds(self, henv, kv):
        # FIXED: upper-case first, then strip the suffix, so "aaa.ns" and "AAA" are one name.
        kv.store[gw.LAST_FULL_SCAN_KEY] = {
            "recommendations": [{"symbol": "aaa.ns", "decision": "BUY NOW"}],
            "all_results": [{"symbol": "AAA", "decision": "BUY NOW"}],
        }
        out = _hot()
        assert out["universe_size"] == 1
        assert "stockky:last_decision:AAA.NS" not in kv.store and "stockky:last_decision:AAA" in kv.store

    def test_all_four_result_keys_are_read(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {
            "recommendations": [{"symbol": "A1", "decision": "BUY NOW"}],
            "recommendations_short": [{"symbol": "A2", "decision": "BUY NOW"}],
            "all_results": [{"symbol": "A3", "decision": "BUY NOW"}],
            "results": [{"symbol": "A4", "decision": "BUY NOW"}],
        }
        assert _hot()["scan_seed_count"] == 4

    def test_a_non_dict_last_scan_is_ignored(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = ["junk"]
        assert _hot()["scan_seed_count"] == 0

    def test_last_scan_read_failure_is_swallowed(self, henv, kv):
        kv.get_raises.add(gw.LAST_FULL_SCAN_KEY)
        henv.watch = ["AAA"]
        out = _hot()
        assert out["scan_seed_count"] == 0 and out["universe_size"] == 1

    def test_seed_write_failure_still_counts_the_symbol(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {"recommendations": [{"symbol": "AAA", "decision": "BUY NOW"}]}
        kv.set_raises.add("stockky:last_decision:")
        out = _hot()
        assert out["scan_seed_count"] == 1 and out["universe_size"] == 1


# ── universe ────────────────────────────────────────────────────────────────

class TestHotUniverse:
    def test_catalyst_sources_come_first_and_duplicates_are_dropped(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {"recommendations": [{"symbol": "S1", "decision": "BUY NOW"}]}
        henv.momentum = ["M1", "M2"]
        henv.news_syms = ["N1"]
        henv.event_syms = ["E1", "M1"]
        henv.watch = ["W1", "N1"]
        henv.searched = ["X1"]
        henv.ipos = ["I1"]
        seen = []
        out = _hot(progress_cb=lambda i, n, s, batch=None: seen.append(s))
        assert seen == ["M1", "M2", "N1", "E1", "S1", "W1", "X1", "I1"]
        assert out["universe_size"] == 8 and out["processed_symbols"] == 8

    def test_each_source_is_capped(self, henv):
        henv.momentum = [f"M{i:03d}" for i in range(100)]
        henv.news_syms = [f"N{i:03d}" for i in range(70)]
        henv.event_syms = [f"E{i:03d}" for i in range(90)]
        henv.searched = [f"X{i:03d}" for i in range(50)]
        henv.ipos = [f"I{i:03d}" for i in range(30)]
        assert _hot()["universe_size"] == 80 + 60 + 80 + 40 + 20

    @pytest.mark.parametrize("attr", ["momentum", "news_syms", "event_syms"])
    def test_a_failing_source_is_logged_and_the_rest_still_scan(self, henv, caplog, attr):
        setattr(henv, attr, RuntimeError("source down"))
        henv.watch = ["AAA"]
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            out = _hot()
        assert out["universe_size"] == 1
        assert "source down" in caplog.text

    def test_none_from_a_source_counts_as_empty(self, henv):
        henv.momentum = None
        henv.watch = ["AAA"]
        assert _hot()["universe_size"] == 1

    def test_empty_universe_falls_back_to_the_nifty_list(self, henv):
        henv.nifty = [f"N{i:03d}" for i in range(100)]
        assert _hot()["universe_size"] == 80

    def test_nifty_is_not_consulted_when_there_is_a_universe(self, henv):
        henv.nifty = ["NIFTY1"]
        henv.watch = ["AAA"]
        seen = []
        _hot(progress_cb=lambda i, n, s, batch=None: seen.append(s))
        assert seen == ["AAA"]

    def test_nothing_at_all_gives_an_empty_but_complete_payload(self, henv, kv):
        henv.nifty = None
        out = _hot()
        assert out["universe_size"] == 0 and out["processed_symbols"] == 0
        assert out["news_driven"] == out["results_driven"] == out["bulk_insider_driven"] == []
        assert any(s[0] == gw.HOT_STOCKS_CACHE_KEY for s in kv.sets)

    @pytest.mark.parametrize("cap,expected", [(3, 10), (10, 10), (15, 15), (500, 30)])
    def test_max_symbols_is_floored_at_ten(self, henv, cap, expected):
        henv.watch = [f"S{i:02d}" for i in range(30)]
        assert _hot(max_symbols=cap)["universe_size"] == expected

    def test_dotted_variants_of_one_symbol_are_deduplicated(self, henv):
        # FIXED: the universe is de-duplicated on the normalised symbol.
        henv.watch = ["AAA", "AAA.NS"]
        henv.events = {"AAA": _results()}
        out = _hot()
        assert out["universe_size"] == 1 and _syms(out["results_driven"]) == ["AAA"]


# ── batching, progress, stop ────────────────────────────────────────────────

class TestHotBatching:
    def test_warm_and_pause_happen_at_each_batch_boundary(self, henv, monkeypatch):
        monkeypatch.setenv("HOT_BATCH_SIZE", "5")
        henv.watch = [f"S{i:02d}" for i in range(12)]
        calls = []
        _hot(progress_cb=lambda i, n, s, batch=None: calls.append((i, n, s, batch)))
        assert len(henv.warm_calls) == 2 and henv.warm_calls[0] is henv.client       # at i=5 and i=10
        assert henv.sleeps == [0.4, 0.4]
        assert [c[3] for c in calls] == [0] * 5 + [1] * 5 + [2] * 2
        assert calls[0] == (0, 12, "S00", 0) and calls[-1] == (11, 12, "S11", 2)

    def test_default_batch_size_is_25(self, henv):
        henv.watch = [f"S{i:02d}" for i in range(30)]
        _hot()
        assert len(henv.warm_calls) == 1

    def test_batch_size_is_floored_at_five(self, henv, monkeypatch):
        monkeypatch.setenv("HOT_BATCH_SIZE", "1")
        henv.watch = [f"S{i:02d}" for i in range(12)]
        _hot()
        assert len(henv.warm_calls) == 2

    def test_hot_batch_size_beats_catalyst_batch_size(self, henv, monkeypatch):
        monkeypatch.setenv("HOT_BATCH_SIZE", "10")
        monkeypatch.setenv("CATALYST_BATCH_SIZE", "5")
        henv.watch = [f"S{i:02d}" for i in range(12)]
        _hot()
        assert len(henv.warm_calls) == 1

    def test_catalyst_batch_size_is_the_fallback(self, henv, monkeypatch):
        monkeypatch.setenv("CATALYST_BATCH_SIZE", "10")
        henv.watch = [f"S{i:02d}" for i in range(12)]
        _hot()
        assert len(henv.warm_calls) == 1

    def test_failing_warm_does_not_stop_the_scan(self, henv, monkeypatch):
        monkeypatch.setenv("HOT_BATCH_SIZE", "5")
        henv.warm_raises = True
        henv.watch = [f"S{i:02d}" for i in range(12)]
        assert _hot()["processed_symbols"] == 12 and henv.sleeps == [0.4, 0.4]


class TestHotProgressCallback:
    def test_callback_without_the_batch_kwarg_is_retried_plain(self, henv):
        henv.watch = ["AAA", "BBB"]
        calls = []
        _hot(progress_cb=lambda i, n, s: calls.append((i, n, s)))
        assert calls == [(0, 2, "AAA"), (1, 2, "BBB")]

    def test_a_callback_that_fails_both_ways_is_ignored(self, henv):
        henv.watch = ["AAA", "BBB"]
        n = []

        def cb(i, total, sym):
            n.append(i)
            raise ValueError("ui gone")

        assert _hot(progress_cb=cb)["processed_symbols"] == 2
        assert n == [0, 1]

    def test_a_callback_that_raises_a_non_type_error_is_ignored(self, henv):
        henv.watch = ["AAA", "BBB"]
        n = []

        def cb(i, total, sym, batch=None):
            n.append(i)
            raise RuntimeError("ui gone")

        assert _hot(progress_cb=cb)["processed_symbols"] == 2
        assert n == [0, 1]                                         # no plain retry for non-TypeErrors


class TestHotStop:
    def test_stop_keeps_the_partial_result_and_shortens_the_ttl(self, henv, kv):
        henv.ttl = 7200
        henv.stop_after = 2
        henv.watch = [f"S{i}" for i in range(5)]
        henv.events = {f"S{i}": _results() for i in range(5)}
        out = _hot()
        assert out["partial"] is True and out["stopped_early"] is True
        assert out["processed_symbols"] == 2 and _syms(out["results_driven"]) == ["S0", "S1"]
        assert out["cache_ttl_seconds"] == 300
        assert out["quality_note"] == ("PARTIAL — scan stopped after 2/5 symbols. "
                                       "Rows shown were fully scored; run again for the rest.")
        assert (gw.HOT_STOCKS_CACHE_KEY, out, 300) in kv.sets

    def test_stop_before_the_first_symbol(self, henv):
        henv.stop_after = 0
        henv.watch = ["AAA", "BBB"]
        out = _hot()
        assert out["processed_symbols"] == 0 and out["stopped_early"] is True
        assert henv.news_calls == []

    def test_partial_ttl_is_configurable(self, henv, monkeypatch):
        monkeypatch.setenv("HOT_PARTIAL_CACHE_TTL", "60")
        henv.ttl = 7200
        henv.stop_after = 1
        henv.watch = ["AAA", "BBB"]
        assert _hot()["cache_ttl_seconds"] == 60

    def test_a_shorter_normal_ttl_is_not_extended(self, henv):
        henv.ttl = 120
        henv.stop_after = 1
        henv.watch = ["AAA", "BBB"]
        assert _hot()["cache_ttl_seconds"] == 120

    def test_a_failing_stop_check_does_not_stop_the_scan(self, henv):
        henv.stop_raises = True
        henv.watch = ["AAA", "BBB", "CCC"]
        out = _hot()
        assert out["stopped_early"] is False and out["processed_symbols"] == 3


# ── per-symbol evaluation ───────────────────────────────────────────────────

class TestHotSymbolBasics:
    def test_symbol_without_any_data_is_processed_but_produces_no_rows(self, henv):
        henv.watch = ["AAA"]
        out = _hot()
        assert out["processed_symbols"] == 1
        assert out["news_driven"] == out["results_driven"] == out["bulk_insider_driven"] == []

    def test_symbols_are_normalised_for_lookups(self, henv):
        henv.watch = ["AAA.NS", "bbb.BO"]
        _hot()
        assert henv.news_calls == ["AAA", "BBB"]

    def test_lowercase_ns_suffix_is_normalised(self, henv):
        # FIXED: upper-case first, then strip the suffix.
        henv.watch = ["aaa.ns"]
        _hot()
        assert henv.news_calls == ["AAA"]

    def test_failing_news_and_event_fetches_are_ignored(self, henv):
        henv.watch = ["AAA"]
        henv.news = {"AAA": RuntimeError("news down")}
        henv.events = {"AAA": RuntimeError("events down")}
        out = _hot()
        assert out["processed_symbols"] == 1 and out["results_driven"] == []

    def test_failing_decision_reads_fall_back_to_do_not_buy(self, henv, kv):
        kv.get_raises |= {"stockky:last_decision:AAA", f"{gw.DECIDE_CACHE_PREFIX}AAA"}
        henv.watch = ["AAA"]
        henv.events = {"AAA": _results()}
        row = _hot()["results_driven"][0]
        assert row["decision"] == "DO NOT BUY" and row["score"] is None

    def test_last_decision_and_decide_cache_feed_the_card(self, henv, kv):
        kv.store["stockky:last_decision:AAA"] = {"decision": "BUY NOW", "score": 71, "reasons": ["a", "b", "c", "d", "e"],
                                                  "combined_score": 71, "target": 120.0}
        kv.store[f"{gw.DECIDE_CACHE_PREFIX}AAA"] = {"combined_score": 75, "stop_loss": 90.0}
        henv.watch = ["AAA"]
        henv.events = {"AAA": _results()}
        row = _hot()["results_driven"][0]
        assert row["decision"] == "BUY NOW" and row["score"] == 71 and row["reasons"] == ["a", "b", "c", "d"]
        assert row["combined_score"] == 75 and row["target"] == 120.0 and row["stop_loss"] == 90.0
        assert row["signal_strength"] == "high"

    def test_reason_dicts_are_flattened(self, henv, kv):
        kv.store["stockky:last_decision:AAA"] = {
            "decision": "BUY NOW", "reasons": {"technical": ["t1", "t2", "t3"], "news": "n1", "empty": ""}}
        henv.watch = ["AAA"]
        henv.events = {"AAA": _results()}
        assert _hot()["results_driven"][0]["reasons"] == ["t1", "t2", "n1"]


class TestHotSections:
    def test_news_section_row(self, henv, kv):
        kv.store["stockky:last_decision:AAA"] = {"decision": "BUY NOW", "score": 72}
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(6, 60, "plain summary", [f"h{i}" for i in range(6)])}
        row = _hot()["news_driven"][0]
        assert row["symbol"] == "AAA" and row["section"] == "news_driven"
        assert row["news_score"] == 60 and row["headline_count"] == 6 and row["summary"] == "plain summary"
        assert row["headlines"] == ["h0", "h1", "h2", "h3", "h4"]
        assert row["signal_strength"] == "high" and row["from_scan"] is False

    @pytest.mark.parametrize("hc,score,extra,kept", [
        (1, 80, {}, False),                    # one headline is never enough
        (3, 54, {}, False),                    # score below 55
        (3, None, {}, False),                  # no score at all
        (2, 60, {}, False),                    # weak news-only: 2 headlines, not actionable / scan / events
        (3, 60, {}, False),                    # 3 headlines is still not enough on its own
        (4, 60, {}, True),                     # 4+ headlines clears the noise gate
    ])
    def test_news_noise_gate(self, henv, hc, score, extra, kept):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(hc, score)}
        out = _hot()
        assert bool(out["news_driven"]) is kept

    def test_weak_news_is_kept_when_the_name_is_actionable_in_the_last_scan(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {"recommendations": [{"symbol": "AAA", "decision": "BUY NOW"}]}
        henv.news = {"AAA": _news(2, 60)}
        row = _hot()["news_driven"][0]
        assert row["from_scan"] is True and row["signal_strength"] == "medium"        # only 2 headlines

    def test_weak_news_is_kept_when_there_is_an_event_signal(self, henv):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(2, 60)}
        henv.events = {"AAA": _results()}
        out = _hot()
        assert _syms(out["news_driven"]) == ["AAA"] and _syms(out["results_driven"]) == ["AAA"]

    def test_unrated_news_gives_a_neutral_medium_row_when_it_has_many_headlines(self, henv):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(4, 60)}
        row = _hot()["news_driven"][0]
        assert row["decision"] == "DO NOT BUY" and row["signal_strength"] == "medium"

    def test_results_section_row(self, henv, kv):
        kv.store["stockky:last_decision:AAA"] = {"decision": "PREPARE TO BUY", "score": 66}
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"next_earnings_date": "2026-10-20", "earnings_surprise": {"surprise_pct": -3.0},
                               "summary": "Q2 due"}}
        row = _hot()["results_driven"][0]
        assert row["section"] == "results_driven" and row["next_earnings_date"] == "2026-10-20"
        assert row["earnings_surprise"] == {"surprise_pct": -3.0} and row["summary"] == "Q2 due"
        assert row["signal_strength"] == "high" and row["decision"] == "PREPARE TO BUY"

    @pytest.mark.parametrize("event", [
        {"next_earnings_date": "2026-10-20"},
        {"earnings_surprise": {"surprise_pct": 0}},
        {"classified_events": [{"event_type": "results"}]},
    ])
    def test_each_results_signal_counts(self, henv, event):
        henv.watch = ["AAA"]
        henv.events = {"AAA": event}
        assert _syms(_hot()["results_driven"]) == ["AAA"]

    def test_events_without_a_results_signal_give_no_results_row(self, henv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"summary": "nothing", "classified_events": [{"event_type": "other"}]}}
        assert _hot()["results_driven"] == []

    def test_bulk_insider_section_row(self, henv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {
            "bulk_deals": [{"d": i} for i in range(5)],
            "recent_insider_transactions": [{"transaction": "Sell"}, {"transaction": None}, {"transaction": "Buy"},
                                            {"transaction": "x"}],
            "summary": "deals"}}
        row = _hot()["bulk_insider_driven"][0]
        assert row["section"] == "bulk_insider_driven" and row["summary"] == "deals"
        assert row["bulk_deals"] == [{"d": 0}, {"d": 1}, {"d": 2}]
        assert len(row["insider_transactions"]) == 3

    @pytest.mark.parametrize("transaction", ["Open Market Purchase", "BUY", "buy"])
    def test_insider_buying_counts(self, henv, transaction):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"recent_insider_transactions": [{"transaction": transaction}]}}
        assert _syms(_hot()["bulk_insider_driven"]) == ["AAA"]

    @pytest.mark.parametrize("event", [
        {"recent_insider_transactions": [{"transaction": "Sell"}]},       # selling alone is not a signal
        {"recent_insider_transactions": [{"transaction": None}]},
        {"classified_events": [{"event_type": "results"}]},
    ])
    def test_non_signals_give_no_bulk_row(self, henv, event):
        henv.watch = ["AAA"]
        henv.events = {"AAA": event}
        assert _hot()["bulk_insider_driven"] == []

    @pytest.mark.parametrize("etype", ["bulk_block", "insider"])
    def test_classified_bulk_or_insider_events_count(self, henv, etype):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"classified_events": [{"event_type": etype}]}}
        assert _syms(_hot()["bulk_insider_driven"]) == ["AAA"]

    def test_one_symbol_can_appear_in_all_three_sections(self, henv):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(4, 60)}
        henv.events = {"AAA": {"next_earnings_date": "2026-10-20", "bulk_deals": [{"x": 1}]}}
        out = _hot()
        assert _syms(out["news_driven"]) == _syms(out["results_driven"]) == _syms(out["bulk_insider_driven"]) == ["AAA"]


class TestHotCatalystPromotion:
    def _promoted(self, kv, sym="AAA"):
        return kv.store[f"stockky:last_decision:{sym}"]

    def test_all_five_catalysts_promote_with_the_capped_score(self, henv, kv):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(3, 70, "Stock set to rally on order win", [f"h{i}" for i in range(6)])}
        henv.events = {"AAA": {"next_earnings_date": "2026-10-20", "earnings_surprise": {"surprise_pct": 12.5},
                               "bulk_deals": [{"x": 1}], "has_positive_catalyst": True}}
        out = _hot()
        bits = ["Positive news flow (score 70, 3 headlines)", "Catalyst language in news summary",
                "Bulk/block deal activity", "Positive earnings surprise 12.5%", "Positive classified catalyst"]
        row = out["results_driven"][0]
        assert row["decision"] == "PREPARE TO BUY" and row["score"] == 70           # 58 + min(12, 5 * 3)
        assert row["reasons"] == bits[:4] and row["signal_strength"] == "high"
        assert self._promoted(kv) == {"decision": "PREPARE TO BUY", "score": 70, "reasons": bits,
                                      "catalyst_promoted": True}
        assert ("stockky:last_decision:AAA", self._promoted(kv), 86400) in kv.sets

    def test_single_catalyst_score_formula(self, henv, kv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"has_positive_catalyst": True}}
        _hot()
        assert self._promoted(kv)["score"] == 61                                     # 58 + 1 * 3

    def test_an_existing_score_is_kept(self, henv, kv):
        kv.store["stockky:last_decision:AAA"] = {"decision": "HOLD", "score": 55}
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"has_positive_catalyst": True}}
        _hot()
        assert self._promoted(kv)["score"] == 55 and self._promoted(kv)["decision"] == "PREPARE TO BUY"

    @pytest.mark.parametrize("decision", ["BUY NOW", "SELL", "PREPARE TO BUY"])
    def test_only_neutral_decisions_are_promoted(self, henv, kv, decision):
        kv.store["stockky:last_decision:AAA"] = {"decision": decision, "score": 60}
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"has_positive_catalyst": True, "next_earnings_date": "x"}}
        out = _hot()
        assert out["results_driven"][0]["decision"] == decision
        assert "catalyst_promoted" not in kv.store["stockky:last_decision:AAA"]

    @pytest.mark.parametrize("decision", ["HOLD", "WAIT", "DO NOT BUY", ""])
    def test_each_neutral_decision_is_promoted(self, henv, kv, decision):
        kv.store["stockky:last_decision:AAA"] = {"decision": decision, "score": 60}
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"has_positive_catalyst": True, "next_earnings_date": "x"}}
        assert _hot()["results_driven"][0]["decision"] == "PREPARE TO BUY"

    def test_positive_news_needs_both_a_high_score_and_two_headlines(self, henv, kv):
        henv.watch = ["AAA", "BBB"]
        henv.news = {"AAA": _news(1, 90), "BBB": _news(3, 61)}
        _hot()
        assert "stockky:last_decision:AAA" not in kv.store and "stockky:last_decision:BBB" not in kv.store

    def test_promotion_write_failure_is_swallowed(self, henv, kv):
        kv.set_raises.add("stockky:last_decision:")
        henv.watch = ["AAA"]
        henv.events = {"AAA": _results(has_positive_catalyst=True)}
        assert _hot()["results_driven"][0]["decision"] == "PREPARE TO BUY"

    def test_negative_earnings_surprise_adds_nothing(self, henv, kv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"earnings_surprise": {"surprise_pct": -5}}}
        out = _hot()
        assert out["results_driven"][0]["decision"] == "DO NOT BUY"

    def test_unparsable_surprise_percent_is_just_a_results_event(self, henv, kv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"earnings_surprise": {"surprise_pct": "n/a"}}}
        _hot()
        assert self._promoted(kv)["reasons"] == ["Earnings/results event"]

    def test_non_dict_earnings_surprise_keeps_the_symbol(self, henv, caplog):
        # FIXED: a truthy non-dict `earnings_surprise` is a results event with no parsable surprise;
        # the symbol is no longer skipped.
        henv.watch = ["AAA", "BBB"]
        henv.events = {"AAA": {"earnings_surprise": "beat"}, "BBB": _results()}
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            out = _hot()
        assert _syms(out["results_driven"]) == ["AAA", "BBB"]
        assert "stockky-hot skip AAA" not in caplog.text

    def test_malformed_news_score_is_treated_as_no_score(self, henv, caplog):
        # FIXED: a non-numeric news score no longer drops the symbol from every section.
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(3, "n/a")}
        henv.events = {"AAA": _results()}
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            out = _hot()
        assert _syms(out["results_driven"]) == ["AAA"] and "stockky-hot skip AAA" not in caplog.text

    @pytest.mark.parametrize("summary", ["Company posts record loss", "Shares fall as it wins no orders",
                                         "Bulk of staff laid off"])
    def test_negative_headlines_are_not_promoted_on_a_substring_accident(self, henv, kv, summary):
        # FIXED: whole-word matching plus a negative-wording veto.
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(1, 40, summary)}
        henv.events = {"AAA": _results()}
        out = _hot()
        assert out["results_driven"][0]["decision"] != "PREPARE TO BUY"
        assert "Catalyst language in news summary" not in (kv.store.get("stockky:last_decision:AAA") or {}).get("reasons", "")

    @pytest.mark.parametrize("summary", ["Stock surges on order win", "Company beats estimates", "Analyst upgrade lifts shares"])
    def test_genuine_catalyst_wording_still_promotes(self, henv, kv, summary):
        henv.watch = ["AAA"]
        henv.news = {"AAA": _news(1, 40, summary)}
        henv.events = {"AAA": _results()}
        out = _hot()
        assert out["results_driven"][0]["decision"] == "PREPARE TO BUY"


# ── price enrichment ────────────────────────────────────────────────────────

class TestHotPrices:
    @pytest.fixture(autouse=True)
    def _two_rows(self, henv):
        henv.watch = ["AAA", "BBB"]
        henv.events = {"AAA": _results(), "BBB": _results()}

    def test_feed_store_prices_are_applied_first(self, henv):
        henv.rows = {"AAA": {"px": 120.5}, "BBB": {"px": 80.0}}
        out = _hot()
        assert [(r["price"], r["close"]) for r in out["results_driven"]] == [(120.5, 120.5), (80.0, 80.0)]
        assert henv.bulk_calls == []

    def test_only_symbols_without_a_feed_price_hit_the_live_waterfall(self, henv):
        henv.rows = {"AAA": {"px": 120.5}}
        henv.bulk_prices = {"BBB": 77.0}
        out = _hot()
        by = {r["symbol"]: r for r in out["results_driven"]}
        assert by["AAA"]["price"] == 120.5 and by["BBB"]["price"] == 77.0 and by["BBB"]["close"] == 77.0
        assert henv.bulk_calls == [["BBB"]]

    @pytest.mark.parametrize("row", [None, {"px": 0.0}, {"px": -3.0}])
    def test_missing_or_non_positive_feed_prices_fall_through_to_the_waterfall(self, henv, row):
        henv.rows = {"AAA": row, "BBB": row}
        henv.bulk_prices = {"AAA": 10.0, "BBB": 20.0}
        out = _hot()
        assert {r["symbol"]: r["price"] for r in out["results_driven"]} == {"AAA": 10.0, "BBB": 20.0}

    def test_a_failing_feed_lookup_skips_only_that_item(self, henv):
        henv.rows = {"AAA": RuntimeError("row down"), "BBB": {"px": 80.0}}
        henv.bulk_prices = {"AAA": 11.0}
        by = {r["symbol"]: r for r in _hot()["results_driven"]}
        assert by["AAA"]["price"] == 11.0 and by["BBB"]["price"] == 80.0

    def test_a_failing_price_resolver_skips_only_that_item(self, henv):
        henv.rows = {"AAA": {"boom": True}, "BBB": {"px": 80.0}}
        henv.bulk_prices = {"AAA": 11.0}
        by = {r["symbol"]: r for r in _hot()["results_driven"]}
        assert by["AAA"]["price"] == 11.0 and by["BBB"]["price"] == 80.0

    def test_unavailable_feed_store_sends_everything_to_the_waterfall(self, henv):
        henv.store_raises = True
        henv.bulk_prices = {"AAA": 10.0, "BBB": 20.0}
        out = _hot()
        assert henv.bulk_calls == [["AAA", "BBB"]]
        assert {r["symbol"]: r["close"] for r in out["results_driven"]} == {"AAA": 10.0, "BBB": 20.0}

    def test_a_symbol_in_two_sections_is_priced_in_both_with_one_lookup(self, henv):
        henv.watch = ["AAA"]
        henv.events = {"AAA": {"next_earnings_date": "x", "bulk_deals": [{"x": 1}]}}
        henv.bulk_prices = {"AAA": 42.0}
        out = _hot()
        assert out["results_driven"][0]["price"] == 42.0 and out["bulk_insider_driven"][0]["price"] == 42.0
        assert henv.bulk_calls == [["AAA"]]

    def test_zero_or_missing_live_prices_leave_the_row_unpriced(self, henv):
        henv.bulk_prices = {"AAA": 0, "BBB": None}
        out = _hot()
        assert all("price" not in r and "close" not in r for r in out["results_driven"])

    def test_empty_waterfall_result_changes_nothing(self, henv):
        henv.bulk_prices = {}
        assert all("price" not in r for r in _hot()["results_driven"])

    def test_waterfall_failure_is_non_fatal(self, henv):
        henv.bulk_raises = RuntimeError("quotes down")
        out = _hot()
        assert len(out["results_driven"]) == 2 and all("price" not in r for r in out["results_driven"])

    def test_one_unparsable_live_price_only_skips_that_item(self, henv):
        # FIXED: the price is parsed per item, so a bad value no longer stops the pass.
        henv.bulk_prices = {"AAA": "n/a", "BBB": 20.0}
        out = _hot()
        by = {r["symbol"]: r for r in out["results_driven"]}
        if "BBB" in by:
            assert by["BBB"].get("price") == 20.0
        if "AAA" in by:
            assert by["AAA"].get("price") != "n/a"

    def test_no_missing_prices_means_no_waterfall_call(self, henv):
        henv.watch = []
        henv.events = {}
        _hot()
        assert henv.bulk_calls == []


# ── ranking, caps, payload, persistence ─────────────────────────────────────

class TestHotRankingAndCaps:
    def test_ranking_prefers_scan_names_then_strength_then_decision_then_score(self, henv, kv):
        kv.store[gw.LAST_FULL_SCAN_KEY] = {"recommendations": [
            {"symbol": "AAA", "decision": "BUY NOW", "combined_score": 70}]}
        kv.store["stockky:last_decision:BBB"] = {"decision": "BUY NOW", "score": 90}
        kv.store["stockky:last_decision:CCC"] = {"decision": "DO NOT BUY", "score": 99}
        kv.store["stockky:last_decision:DDD"] = {"decision": "PREPARE TO BUY", "score": 80}
        kv.store["stockky:last_decision:EEE"] = {"decision": "BUY NOW", "score": 60}
        henv.watch = ["CCC", "DDD", "BBB", "EEE"]
        henv.events = {s: _results() for s in ("AAA", "BBB", "CCC", "DDD", "EEE")}
        out = _hot()
        # scan name first; then "high" strength (actionable): BUY NOW by score, then PREPARE; DO NOT BUY last
        assert _syms(out["results_driven"]) == ["AAA", "BBB", "EEE", "DDD", "CCC"]

    def test_ties_break_on_score_then_headline_count(self, henv, kv):
        for s, sc in (("AAA", 60), ("BBB", 60), ("CCC", 70)):
            kv.store[f"stockky:last_decision:{s}"] = {"decision": "BUY NOW", "score": sc}
        henv.watch = ["AAA", "BBB", "CCC"]
        henv.news = {"AAA": _news(3, 60), "BBB": _news(5, 60), "CCC": _news(3, 60)}
        out = _hot()
        assert _syms(out["news_driven"]) == ["CCC", "BBB", "AAA"]

    def test_section_caps(self, henv):
        names = [f"S{i:02d}" for i in range(15)]
        henv.watch = names
        henv.events = {n: {"next_earnings_date": "x", "bulk_deals": [{"x": 1}]} for n in names}
        henv.news = {n: _news(4, 60) for n in names}
        out = _hot()
        assert len(out["news_driven"]) == 10
        assert len(out["results_driven"]) == 12 and len(out["bulk_insider_driven"]) == 12


class TestHotPayload:
    def test_payload_shape_and_cache_write(self, henv, kv):
        henv.ttl = 180
        henv.phase = "open"
        henv.watch = ["AAA", "BBB"]
        henv.events = {"AAA": _results()}
        out = _hot()
        assert out["cached"] is False and out["partial"] is False and out["stopped_early"] is False
        assert out["universe_size"] == 2 and out["processed_symbols"] == 2 and out["scan_seed_count"] == 0
        assert out["cache_ttl_seconds"] == 180 and out["market_phase"] == "open"
        assert out["quality_note"] == (
            "Ranked by scan BUY/PREPARE, bulk/insider, results first; weak news-only names dropped.")
        assert datetime.fromisoformat(out["generated_at"]).utcoffset() == timedelta(hours=5, minutes=30)
        assert out["fingerprint"] == gw._hot_payload_fingerprint(out) != ""
        assert (gw.HOT_STOCKS_CACHE_KEY, out, 180) in kv.sets

    def test_returned_payload_is_the_cached_object_with_a_fresh_cache_flag_on_the_next_call(self, henv, kv):
        henv.watch = ["AAA"]
        first = _hot()
        second = _hot()
        assert second == {**first, "cached": True}

    def test_durable_upsert_receives_the_payload(self, henv):
        henv.upsert = 3
        henv.watch = ["AAA"]
        out = _hot()
        assert henv.upsert_calls == [out]

    def test_upsert_returning_zero_is_fine(self, henv):
        henv.upsert = 0
        assert _hot()["cached"] is False

    def test_upsert_failure_never_fails_the_scan(self, henv):
        henv.upsert = RuntimeError("db down")
        henv.watch = ["AAA"]
        out = _hot()
        assert out["processed_symbols"] == 1


class TestPass82Helpers:
    def test_catalyst_language_helper_handles_empty_and_negative_text(self):
        assert gw._has_catalyst_language("") is False and gw._has_catalyst_language(None) is False
        assert gw._has_catalyst_language("Stock surges on order win") is True
        assert gw._has_catalyst_language("Stock surges but company posts a loss") is False

    def test_a_malformed_insider_row_skips_only_that_symbol(self, henv, caplog):
        henv.watch = ["AAA", "BBB"]
        henv.events = {"AAA": {"bulk_deals": [{"d": 1}], "recent_insider_transactions": [1], "summary": "x"},
                       "BBB": _results()}
        with caplog.at_level(logging.WARNING, logger=gw.logger.name):
            out = _hot()
        assert "stockky-hot skip AAA" in caplog.text and "BBB" in _syms(out["results_driven"])
