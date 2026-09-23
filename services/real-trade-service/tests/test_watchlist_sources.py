"""
tests/test_watchlist_sources.py  — session89, step 2
=====================================================
Coverage target:
  watchlist_engine/sources.py   0% → 100%   (108 statements)

Approach: patch httpx.AsyncClient and the two circuit breakers' `.call()`
method so no real HTTP ever fires.  Also patch local_cache and
_fetch_volume_shock_universe for Tier 3 tests.

Run from services/real-trade-service:
    python3 -m pytest tests/test_watchlist_sources.py -v \
        --cov=watchlist_engine.sources --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.sources as src


def run(coro):
    return asyncio.run(coro)


# ── shared fake db (local_cache calls need it; we mock save/load so DB
#    doesn't have to be real) ──────────────────────────────────────────────

_FAKE_DB = object()


# ══════════════════════════════════════════════════════════════════════════════
# _parse_ts tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNow:
    """Line 47 — _now() returns a tz-aware UTC datetime."""

    def test_now_returns_aware_utc(self):
        result = src._now()
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_now_is_recent(self):
        from datetime import timedelta
        result = src._now()
        delta = abs((result - datetime.now(timezone.utc)).total_seconds())
        assert delta < 2


class TestParseTs:
    def test_none_returns_none(self):
        assert src._parse_ts(None) is None

    def test_datetime_passthrough(self):
        dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
        assert src._parse_ts(dt) is dt

    def test_epoch_int(self):
        result = src._parse_ts(0)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_epoch_float(self):
        result = src._parse_ts(1_700_000_000.5)
        assert result is not None

    def test_iso_string_with_time(self):
        result = src._parse_ts("2026-09-01T09:15:00")
        assert result is not None
        assert result.year == 2026
        assert result.month == 9
        assert result.day == 1

    def test_date_string_only(self):
        result = src._parse_ts("2026-09-15")
        assert result is not None
        assert result.day == 15

    def test_unparseable_string_returns_none(self):
        assert src._parse_ts("not-a-date") is None

    def test_bad_epoch_returns_none(self):
        assert src._parse_ts(9_999_999_999_999) is None  # too large for fromtimestamp

    def test_list_value_returns_none(self):
        assert src._parse_ts([2026, 9, 1]) is None


# ══════════════════════════════════════════════════════════════════════════════
# _normalize_tier1 tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeTier1:
    def _payload(self, hot=None, ipo=None):
        return {"hot_picks": hot or {}, "ipo": ipo or {}}

    def test_empty_payload_returns_empty(self):
        assert src._normalize_tier1({}) == []

    def test_hot_picks_bulk_insider_bucket(self):
        payload = self._payload(hot={
            "generated_at": "2026-09-01T10:00:00",
            "bulk_insider_driven": [
                {"symbol": "TCS", "price": 3000.0, "score": 0.9},
            ],
        })
        result = src._normalize_tier1(payload)
        assert len(result) == 1
        assert result[0]["symbol"] == "TCS"
        assert result[0]["catalyst_type"] == "bulk_block"
        assert result[0]["source_tier"] == 1
        assert result[0]["conviction_score"] == pytest.approx(0.9)
        assert result[0]["catalyst_price_source"] == "live"

    def test_hot_picks_results_bucket(self):
        payload = self._payload(hot={
            "results_driven": [{"symbol": "reliance", "price": 2500.0}],
        })
        result = src._normalize_tier1(payload)
        assert result[0]["symbol"] == "RELIANCE"
        assert result[0]["catalyst_type"] == "results"

    def test_hot_picks_news_bucket_maps_to_board(self):
        payload = self._payload(hot={
            "news_driven": [{"symbol": "INFY", "close": 1500.0}],
        })
        result = src._normalize_tier1(payload)
        assert result[0]["catalyst_type"] == "board"
        assert result[0]["catalyst_price"] == 1500.0
        assert result[0]["catalyst_price_source"] == "close"  # no price, fell back to close

    def test_hot_picks_missing_price_and_close_is_unknown(self):
        payload = self._payload(hot={
            "bulk_insider_driven": [{"symbol": "WIPRO"}],  # no price, no close
        })
        result = src._normalize_tier1(payload)
        assert result[0]["catalyst_price"] is None
        assert result[0]["catalyst_price_source"] == "unknown"

    def test_hot_picks_empty_symbol_skipped(self):
        payload = self._payload(hot={
            "bulk_insider_driven": [{"symbol": "", "price": 100.0}],
        })
        assert src._normalize_tier1(payload) == []

    def test_ipo_list_as_list(self):
        payload = self._payload(ipo=[
            {"symbol": "NEWCO", "current_price": 250.0, "ipo_score": 0.75,
             "listing_date": "2026-09-20"},
        ])
        result = src._normalize_tier1(payload)
        # Only IPO rows (no hot buckets populated)
        ipo_rows = [r for r in result if r["catalyst_type"] == "ipo"]
        assert len(ipo_rows) == 1
        assert ipo_rows[0]["symbol"] == "NEWCO"
        assert ipo_rows[0]["conviction_score"] == pytest.approx(0.75)
        assert ipo_rows[0]["catalyst_price"] == 250.0
        assert ipo_rows[0]["catalyst_price_source"] == "live"

    def test_ipo_list_under_results_key(self):
        payload = self._payload(ipo={"results": [
            {"symbol": "IROFC", "current_price": 100.0},
        ]})
        result = src._normalize_tier1(payload)
        ipo_rows = [r for r in result if r["catalyst_type"] == "ipo"]
        assert len(ipo_rows) == 1
        assert ipo_rows[0]["symbol"] == "IROFC"

    def test_ipo_list_under_items_key(self):
        payload = self._payload(ipo={"items": [
            {"symbol": "METAFORCE", "current_price": 50.0},
        ]})
        result = src._normalize_tier1(payload)
        ipo_rows = [r for r in result if r["catalyst_type"] == "ipo"]
        assert len(ipo_rows) == 1

    def test_ipo_fallback_to_score_field(self):
        payload = self._payload(ipo=[
            {"symbol": "OLD", "current_price": 100.0, "score": 0.5},  # no ipo_score
        ])
        result = src._normalize_tier1(payload)
        ipo_row = next(r for r in result if r["catalyst_type"] == "ipo")
        assert ipo_row["conviction_score"] == pytest.approx(0.5)

    def test_ipo_price_fallback_chain(self):
        # current_price absent, cmp present
        payload = self._payload(ipo=[
            {"symbol": "PRICETEST", "cmp": 150.0},
        ])
        result = src._normalize_tier1(payload)
        ipo_row = next(r for r in result if r["catalyst_type"] == "ipo")
        assert ipo_row["catalyst_price"] == 150.0

    def test_ipo_empty_symbol_skipped(self):
        payload = self._payload(ipo=[{"symbol": "", "current_price": 100.0}])
        result = src._normalize_tier1(payload)
        assert not any(r["catalyst_type"] == "ipo" for r in result)

    def test_ipo_no_price_is_unknown_source(self):
        payload = self._payload(ipo=[{"symbol": "NOPRICE"}])
        result = src._normalize_tier1(payload)
        ipo_row = next(r for r in result if r["catalyst_type"] == "ipo")
        assert ipo_row["catalyst_price_source"] == "unknown"

    def test_generated_at_none_does_not_crash(self):
        payload = self._payload(hot={
            "results_driven": [{"symbol": "SAFE", "price": 100.0}],
        })
        # hot dict has no generated_at — _parse_ts(None) should return None
        result = src._normalize_tier1(payload)
        assert result[0]["catalyst_ts"] is None

    def test_hot_picks_none_bucket_skipped(self):
        payload = self._payload(hot={
            "bulk_insider_driven": None,  # explicitly None
        })
        assert src._normalize_tier1(payload) == []


# ══════════════════════════════════════════════════════════════════════════════
# _classify_tier2 tests
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyTier2:
    def test_empty_payload_returns_empty(self):
        assert src._classify_tier2({}) == []

    def test_headline_with_match_adds_candidate(self):
        fake_classify = MagicMock(return_value=["board"])
        with patch("event_depth_local.classify_text", fake_classify):
            result = src._classify_tier2({
                "items": [{"symbol": "ICICI", "headline": "Board declares dividend", "price": 500.0}]
            })
        assert len(result) == 1
        assert result[0]["symbol"] == "ICICI"
        assert result[0]["catalyst_type"] == "board"
        assert result[0]["source_tier"] == 2
        assert result[0]["conviction_score"] is None
        assert result[0]["catalyst_price_source"] == "live"

    def test_no_headline_skipped(self):
        with patch("event_depth_local.classify_text", return_value=["board"]):
            result = src._classify_tier2({
                "items": [{"symbol": "HDFC"}]  # no headline
            })
        assert result == []

    def test_no_symbol_skipped(self):
        with patch("event_depth_local.classify_text", return_value=["board"]):
            result = src._classify_tier2({
                "items": [{"headline": "big news"}]  # no symbol
            })
        assert result == []

    def test_empty_tags_skipped(self):
        with patch("event_depth_local.classify_text", return_value=[]):
            result = src._classify_tier2({
                "items": [{"symbol": "KOTAK", "headline": "nothing special"}]
            })
        assert result == []

    def test_ts_field_used(self):
        ts_str = "2026-09-01T09:15:00"
        with patch("event_depth_local.classify_text", return_value=["ipo"]):
            result = src._classify_tier2({
                "items": [{"symbol": "NEW", "headline": "IPO listing", "ts": ts_str}]
            })
        assert result[0]["catalyst_ts"] is not None

    def test_detected_at_fallback(self):
        with patch("event_depth_local.classify_text", return_value=["insider"]):
            result = src._classify_tier2({
                "items": [{"symbol": "BSE", "headline": "insider buy",
                            "detected_at": "2026-09-02T10:00:00"}]
            })
        assert result[0]["catalyst_ts"] is not None

    def test_no_price_is_unknown_source(self):
        with patch("event_depth_local.classify_text", return_value=["board"]):
            result = src._classify_tier2({
                "items": [{"symbol": "NSE", "headline": "AGM result"}]
            })
        assert result[0]["catalyst_price_source"] == "unknown"
        assert result[0]["catalyst_price"] is None

    def test_multiple_items(self):
        with patch("event_depth_local.classify_text", return_value=["board"]):
            result = src._classify_tier2({
                "items": [
                    {"symbol": "A", "headline": "news A"},
                    {"symbol": "B", "headline": "news B"},
                ]
            })
        assert len(result) == 2

    def test_title_field_used_when_headline_absent(self):
        with patch("event_depth_local.classify_text", return_value=["board"]) as m:
            result = src._classify_tier2({
                "items": [{"symbol": "ANGEL", "title": "big board meeting"}]
            })
        # classify_text was called with the title value
        m.assert_called_once_with("big board meeting")
        assert len(result) == 1


# ══════════════════════════════════════════════════════════════════════════════
# _tier3_volume_shock tests
# ══════════════════════════════════════════════════════════════════════════════

class TestTier3VolumeShock:
    def test_returns_formatted_candidates(self):
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["RELIANCE", "tcs"])):
            result = run(src._tier3_volume_shock())
        assert len(result) == 2
        syms = {r["symbol"] for r in result}
        assert "RELIANCE" in syms
        assert "TCS" in syms
        for r in result:
            assert r["catalyst_type"] == "volume_shock"
            assert r["source_tier"] == 3
            assert r["catalyst_price"] is None
            assert r["conviction_score"] is None

    def test_exception_returns_empty(self):
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(side_effect=RuntimeError("yfinance down"))):
            result = run(src._tier3_volume_shock())
        assert result == []

    def test_empty_universe_returns_empty(self):
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=[])):
            result = run(src._tier3_volume_shock())
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# fetch_watchlist_candidates — full ladder tests
# ══════════════════════════════════════════════════════════════════════════════

def _make_breaker_that_calls_fn():
    """Return a mock CircuitBreaker that always calls fn() directly."""
    async def _call(fn, fallback=None):
        try:
            return await fn()
        except Exception:
            return await fallback() if fallback else None
    cb = MagicMock()
    cb.call = _call
    return cb


def _make_breaker_that_uses_fallback():
    """Return a mock CircuitBreaker that always skips fn and calls fallback.
    The real breaker's fallback is always an async callable; the lambda:None
    in sources.py is sync so we must handle both."""
    async def _call(fn, fallback=None):
        if fallback is None:
            return None
        result = fallback()
        if asyncio.iscoroutine(result):
            return await result
        return result
    cb = MagicMock()
    cb.call = _call
    return cb


class TestFetchWatchlistCandidatesTier1:
    def _hot_payload(self):
        return {
            "bulk_insider_driven": [{"symbol": "TCS", "price": 3000.0, "score": 0.9}],
            "generated_at": "2026-09-01T10:00:00",
        }

    def _ipo_payload(self):
        return [{"symbol": "NEWIPO", "current_price": 200.0, "ipo_score": 0.8}]

    def test_tier1_hit_returns_candidates(self):
        fake_hot = MagicMock()
        fake_hot.json.return_value = self._hot_payload()
        fake_hot.raise_for_status = MagicMock()
        fake_ipo = MagicMock()
        fake_ipo.json.return_value = self._ipo_payload()
        fake_ipo.raise_for_status = MagicMock()

        fake_client = AsyncMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.get = AsyncMock(side_effect=[fake_hot, fake_ipo])

        with patch("httpx.AsyncClient", return_value=fake_client), \
             patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.save_snapshot", return_value=None), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))

        assert len(result) > 0
        syms = {r["symbol"] for r in result}
        assert "TCS" in syms

    def test_tier1_empty_result_falls_to_tier2(self):
        # Tier 1 returns empty hot/ipo
        fake_hot = MagicMock()
        fake_hot.json.return_value = {}
        fake_hot.raise_for_status = MagicMock()
        fake_ipo = MagicMock()
        fake_ipo.json.return_value = []
        fake_ipo.raise_for_status = MagicMock()

        fake_client = AsyncMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.get = AsyncMock(side_effect=[fake_hot, fake_ipo,
                                                   # Tier 2 call
                                                   AsyncMock(json=lambda: {"items": []},
                                                             raise_for_status=MagicMock())])

        # Tier 2's client.get for /events/raw-feed
        tier2_resp = MagicMock()
        tier2_resp.json.return_value = {"items": []}
        tier2_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient", return_value=fake_client), \
             patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.event_service_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.save_snapshot"), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None), \
             patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["VOLSHOCK"])):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))

        # Falls to Tier 3 (volume_shock) since Tier 2 items was empty
        assert any(r["catalyst_type"] == "volume_shock" for r in result)


class TestFetchWatchlistCandidatesTier2:
    def test_tier1_breaker_open_uses_cache_if_present(self):
        # Breaker is open → calls fallback → load_snapshot returns cached data
        cached = {
            "hot_picks": {"bulk_insider_driven": [{"symbol": "CACHED", "price": 1.0, "score": 0.5}],
                          "generated_at": None},
            "ipo": [],
        }
        with patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.load_snapshot", return_value=cached), \
             patch("watchlist_engine.sources.save_snapshot"):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))
        assert any(r["symbol"] == "CACHED" for r in result)

    def test_tier1_breaker_open_no_cache_tries_tier2(self):
        # Tier 1 breaker open, no cache → fallback returns None → Tier 2 attempted
        tier2_resp = MagicMock()
        tier2_resp.json.return_value = {"items": [
            {"symbol": "EVT", "headline": "earnings beat", "price": 200.0}
        ]}
        tier2_resp.raise_for_status = MagicMock()

        fake_client = AsyncMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.get = AsyncMock(return_value=tier2_resp)

        with patch("httpx.AsyncClient", return_value=fake_client), \
             patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.event_service_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None), \
             patch("event_depth_local.classify_text", return_value=["results"]):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))

        assert any(r["symbol"] == "EVT" for r in result)

    def test_tier2_breaker_open_falls_to_tier3(self):
        with patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.event_service_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None), \
             patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["T3SYM"])):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))

        assert any(r["symbol"] == "T3SYM" for r in result)
        assert all(r["source_tier"] == 3 for r in result)


class TestFetchWatchlistCandidatesTier3:
    def test_all_tiers_empty_tier3_volume_shock_returned(self):
        fake_hot = MagicMock()
        fake_hot.json.return_value = {}
        fake_hot.raise_for_status = MagicMock()
        fake_ipo = MagicMock()
        fake_ipo.json.return_value = []
        fake_ipo.raise_for_status = MagicMock()

        tier2_resp = MagicMock()
        tier2_resp.json.return_value = {}  # no items
        tier2_resp.raise_for_status = MagicMock()

        call_count = [0]
        async def _multi_get(url, **kwargs):
            call_count[0] += 1
            if "/stockky-hot" in url:
                return fake_hot
            if "/ipo/list" in url:
                return fake_ipo
            return tier2_resp

        fake_client = AsyncMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.get = AsyncMock(side_effect=_multi_get)

        with patch("httpx.AsyncClient", return_value=fake_client), \
             patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.event_service_breaker",
                   _make_breaker_that_calls_fn()), \
             patch("watchlist_engine.sources.save_snapshot"), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None), \
             patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(return_value=["VOLX", "VOLY"])):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))

        vol_rows = [r for r in result if r["catalyst_type"] == "volume_shock"]
        assert len(vol_rows) == 2
        assert {r["symbol"] for r in vol_rows} == {"VOLX", "VOLY"}

    def test_tier3_exception_returns_empty(self):
        with patch("watchlist_engine.sources.api_gateway_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.event_service_breaker",
                   _make_breaker_that_uses_fallback()), \
             patch("watchlist_engine.sources.load_snapshot", return_value=None), \
             patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(side_effect=RuntimeError("total failure"))):
            result = run(src.fetch_watchlist_candidates(_FAKE_DB, "DEMO"))
        assert result == []
