"""
tests/test_peer_multi_quarter.py — fundamental/peer_multi_quarter.py
httpx.get monkeypatched. No real network.
"""
from __future__ import annotations
import os, sys, time, threading, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest
import peer_multi_quarter as pmq


@pytest.fixture(autouse=True)
def _clean_cache():
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()
    yield
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()


# ── _safe ─────────────────────────────────────────────────────────────────────

class TestSafe:
    def test_normal_float(self):
        assert pmq._safe(3.14) == pytest.approx(3.14)

    def test_none_returns_default(self):
        assert pmq._safe(None) == 0.0

    def test_nan_returns_default(self):
        assert pmq._safe(float("nan")) == 0.0

    def test_string_number(self):
        assert pmq._safe("25.5") == pytest.approx(25.5)

    def test_bad_string_returns_default(self):
        assert pmq._safe("N/A") == 0.0

    def test_custom_default(self):
        assert pmq._safe(None, default=99.0) == 99.0


# ── _norm_symbol ──────────────────────────────────────────────────────────────

class TestNormSymbol:
    def test_adds_ns(self):
        assert pmq._norm_symbol("RELIANCE") == "RELIANCE.NS"

    def test_preserves_ns(self):
        assert pmq._norm_symbol("TCS.NS") == "TCS.NS"

    def test_preserves_bo(self):
        assert pmq._norm_symbol("INFY.BO") == "INFY.BO"

    def test_uppercases(self):
        assert pmq._norm_symbol("reliance") == "RELIANCE.NS"


# ── detect_sector ─────────────────────────────────────────────────────────────

class TestDetectSector:
    def test_it_from_software(self):
        assert pmq.detect_sector({"sector": "software services"}) == "IT"

    def test_bank_from_financial(self):
        assert pmq.detect_sector({"sector": "Financial Services"}) == "BANK"

    def test_auto(self):
        assert pmq.detect_sector({"industry": "Automobile Components"}) == "AUTO"

    def test_pharma(self):
        assert pmq.detect_sector({"sector": "Drug Manufacturers"}) == "PHARMA"

    def test_fmcg(self):
        assert pmq.detect_sector({"sector": "Consumer Goods FMCG"}) == "FMCG"

    def test_metal(self):
        assert pmq.detect_sector({"sector": "Steel and Metal Mining"}) == "METAL"

    def test_energy(self):
        assert pmq.detect_sector({"sector": "Oil and Gas Energy"}) == "ENERGY"

    def test_unknown_returns_default(self):
        assert pmq.detect_sector({"sector": "Agriculture"}) == "DEFAULT"

    def test_empty_returns_default(self):
        assert pmq.detect_sector({}) == "DEFAULT"

    def test_sectordisp_key(self):
        assert pmq.detect_sector({"sectorDisp": "Technology"}) == "IT"


# ── _fund_cache_get / _fund_cache_set ────────────────────────────────────────

class TestFundCache:
    def test_miss_returns_none(self):
        assert pmq._fund_cache_get("UNKNOWN.NS") is None

    def test_set_and_get(self):
        pmq._fund_cache_set("TCS.NS", {"pe": 25})
        assert pmq._fund_cache_get("TCS.NS") == {"pe": 25}

    def test_expired_returns_none(self):
        with pmq._FUND_CACHE_LOCK:
            pmq._FUND_CACHE["INFY.NS"] = (time.time() - pmq._FUND_CACHE_TTL - 1, {"pe": 20})
        assert pmq._fund_cache_get("INFY.NS") is None

    def test_thread_safe(self):
        errors = []
        def _writer(i):
            try:
                pmq._fund_cache_set(f"SYM{i}.NS", {"i": i})
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors


# ── fetch_fundamentals ────────────────────────────────────────────────────────

class TestFetchFundamentals:
    def test_returns_cached_without_http(self, monkeypatch):
        pmq._fund_cache_set("TCS.NS", {"pe": 30})
        import httpx
        monkeypatch.setattr(httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call")))
        result = pmq.fetch_fundamentals("http://mds/", "TCS.NS")
        assert result["pe"] == 30

    def test_fetches_and_caches_on_200(self, monkeypatch):
        import httpx
        class _R:
            status_code = 200
            def json(self): return {"pe": 22}
        monkeypatch.setattr(httpx, "get", lambda url, timeout: _R())
        result = pmq.fetch_fundamentals("http://mds/", "WIPRO.NS")
        assert result["pe"] == 22
        assert pmq._fund_cache_get("WIPRO.NS") == {"pe": 22}

    def test_returns_empty_on_non_200(self, monkeypatch):
        import httpx
        class _R:
            status_code = 404
            def json(self): return {}
        monkeypatch.setattr(httpx, "get", lambda url, timeout: _R())
        result = pmq.fetch_fundamentals("http://mds/", "MISSING.NS")
        assert result == {}

    def test_returns_empty_on_exception(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
        result = pmq.fetch_fundamentals("http://mds/", "BROKEN.NS")
        assert result == {}


# ── fetch_fundamentals_batch ─────────────────────────────────────────────────

class TestFetchFundamentalsBatch:
    def test_all_from_cache(self, monkeypatch):
        pmq._fund_cache_set("TCS.NS", {"pe": 30})
        pmq._fund_cache_set("INFY.NS", {"pe": 28})
        import httpx
        monkeypatch.setattr(httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call")))
        result = pmq.fetch_fundamentals_batch("http://mds/", ["TCS.NS", "INFY.NS"])
        assert result["TCS.NS"]["pe"] == 30
        assert result["INFY.NS"]["pe"] == 28

    def test_empty_list_returns_empty(self, monkeypatch):
        result = pmq.fetch_fundamentals_batch("http://mds/", [])
        assert result == {}

    def test_fetches_uncached_in_parallel(self, monkeypatch):
        import httpx
        responses = {"A.NS": {"pe": 10}, "B.NS": {"pe": 20}}
        class _R:
            def __init__(self, sym):
                self.status_code = 200
                self._data = responses.get(sym, {})
            def json(self): return self._data
        monkeypatch.setattr(httpx, "get", lambda url, timeout: _R(url.split("/")[-1]))
        result = pmq.fetch_fundamentals_batch("http://mds/", ["A.NS", "B.NS"])
        assert "A.NS" in result and "B.NS" in result

    def test_failed_fetch_maps_to_empty(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("x")))
        result = pmq.fetch_fundamentals_batch("http://mds/", ["BAD.NS"])
        assert result["BAD.NS"] == {}


# ── compute_peer_relative ─────────────────────────────────────────────────────

class TestComputePeerRelative:
    def _patch_batch(self, monkeypatch, peer_data):
        monkeypatch.setattr(pmq, "fetch_fundamentals_batch",
            lambda url, syms, timeout=15: {s: peer_data for s in syms})

    def test_returns_expected_keys(self, monkeypatch):
        self._patch_batch(monkeypatch, {"pe_ratio": 25.0, "roe": 15.0,
                                         "revenue_growth_yoy": 10.0, "profit_growth_yoy": 8.0})
        result = pmq.compute_peer_relative("RELIANCE", {"pe_ratio": 20.0}, "http://mds/")
        assert "peer_score" in result
        assert "sector" in result
        assert "relative" in result

    def test_cheaper_pe_raises_score(self, monkeypatch):
        self._patch_batch(monkeypatch, {"pe_ratio": 30.0})
        result = pmq.compute_peer_relative("X", {"pe_ratio": 10.0}, "http://mds/")
        assert result["peer_score"] > 50.0

    def test_expensive_pe_lowers_score(self, monkeypatch):
        self._patch_batch(monkeypatch, {"pe_ratio": 10.0})
        result = pmq.compute_peer_relative("X", {"pe_ratio": 50.0}, "http://mds/")
        assert result["peer_score"] < 50.0

    def test_empty_peer_data_returns_neutral(self, monkeypatch):
        monkeypatch.setattr(pmq, "fetch_fundamentals_batch",
            lambda url, syms, timeout=15: {})
        result = pmq.compute_peer_relative("X", {}, "http://mds/")
        assert result["peer_score"] == pytest.approx(50.0)

    def test_self_excluded_from_peer_list(self, monkeypatch):
        seen_symbols = []
        def _fake_batch(url, syms, timeout=15):
            seen_symbols.extend(syms)
            return {}
        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", _fake_batch)
        pmq.compute_peer_relative("RELIANCE", {"sector": "ENERGY"}, "http://mds/")
        assert "RELIANCE.NS" not in seen_symbols

    def test_sector_detection_used(self, monkeypatch):
        self._patch_batch(monkeypatch, {})
        result = pmq.compute_peer_relative("TCS", {"sector": "Software"}, "http://mds/")
        assert result["sector"] == "IT"

    def test_score_clamped_0_100(self, monkeypatch):
        self._patch_batch(monkeypatch, {"pe_ratio": 0.1, "roe": 0.0})
        result = pmq.compute_peer_relative("X", {"pe_ratio": 1000.0}, "http://mds/")
        assert 0.0 <= result["peer_score"] <= 100.0


# ── compute_multi_quarter_consistency ─────────────────────────────────────────

class TestComputeMultiQuarterConsistency:
    def test_empty_quarterly_and_no_fundamentals(self):
        result = pmq.compute_multi_quarter_consistency()
        assert result["quarters_checked"] == 0
        assert result["consistency_score"] == 50.0

    def test_two_positive_quarters(self):
        q = [
            {"period": "Q1", "revenue_growth": 12.0, "profit_growth": 8.0},
            {"period": "Q2", "revenue_growth": 10.0, "profit_growth": 6.0},
        ]
        result = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert result["consistent_revenue"] is True
        assert result["consistent_profit"] is True
        assert result["consistent_both"] is True
        assert result["consistency_score"] > 50.0

    def test_mixed_quarters_inconsistent(self):
        q = [
            {"period": "Q1", "revenue_growth": 5.0, "profit_growth": -3.0},
            {"period": "Q2", "revenue_growth": -2.0, "profit_growth": 4.0},
        ]
        result = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert result["consistent_both"] is False

    def test_falls_back_to_fundamentals(self):
        fund = {"revenue_growth_yoy": 15.0, "profit_growth_yoy": 10.0}
        result = pmq.compute_multi_quarter_consistency(fundamentals=fund)
        assert result["quarters_checked"] == 1
        assert result["avg_revenue_growth"] == pytest.approx(15.0)

    def test_zero_yoy_not_added_as_fallback(self):
        fund = {"revenue_growth_yoy": 0.0, "profit_growth_yoy": 0.0}
        result = pmq.compute_multi_quarter_consistency(fundamentals=fund)
        assert result["quarters_checked"] == 0

    def test_high_growth_bonus(self):
        q = [{"period": f"Q{i}", "revenue_growth": 20.0, "profit_growth": 15.0}
             for i in range(3)]
        result = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert result["consistency_score"] > 95.0

    def test_alternative_keys_accepted(self):
        q = [{"date": "Q1", "revenueGrowth": 8.0, "earningsGrowth": 5.0}]
        result = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert result["quarters_checked"] == 1

    def test_score_clamped_0_100(self):
        q = [{"period": "Q1", "revenue_growth": -99.0, "profit_growth": -99.0}]
        result = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert 0.0 <= result["consistency_score"] <= 100.0


# ── enrich_fundamentals_with_peer_and_consistency ────────────────────────────

class TestEnrichFundamentals:
    def test_adds_peer_relative_and_multi_quarter(self, monkeypatch):
        monkeypatch.setattr(pmq, "compute_peer_relative",
            lambda *a, **kw: {"peer_score": 65.0})
        monkeypatch.setattr(pmq, "compute_multi_quarter_consistency",
            lambda **kw: {"consistency_score": 70.0, "consistent_both": True})
        result = pmq.enrich_fundamentals_with_peer_and_consistency(
            "RELIANCE", {"pe": 20}, "http://mds/")
        assert result["peer_score"] == 65.0
        assert result["consistency_score"] == 70.0
        assert result["consistent_growth"] is True

    def test_peer_error_returns_50_default(self, monkeypatch):
        monkeypatch.setattr(pmq, "compute_peer_relative",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("network down")))
        monkeypatch.setattr(pmq, "compute_multi_quarter_consistency",
            lambda **kw: {"consistency_score": 50.0, "consistent_both": False})
        result = pmq.enrich_fundamentals_with_peer_and_consistency(
            "X", {}, "http://mds/")
        assert result["peer_score"] == 50.0

    def test_multi_quarter_error_returns_50_default(self, monkeypatch):
        monkeypatch.setattr(pmq, "compute_peer_relative",
            lambda *a, **kw: {"peer_score": 60.0})
        monkeypatch.setattr(pmq, "compute_multi_quarter_consistency",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("calc error")))
        result = pmq.enrich_fundamentals_with_peer_and_consistency(
            "X", {}, "http://mds/")
        assert result["consistency_score"] == 50.0

    def test_original_fields_preserved(self, monkeypatch):
        monkeypatch.setattr(pmq, "compute_peer_relative",
            lambda *a, **kw: {"peer_score": 55.0})
        monkeypatch.setattr(pmq, "compute_multi_quarter_consistency",
            lambda **kw: {"consistency_score": 55.0, "consistent_both": False})
        result = pmq.enrich_fundamentals_with_peer_and_consistency(
            "X", {"custom": "value"}, "http://mds/")
        assert result["custom"] == "value"
