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


class TestDetectSectorWordMatching:
    """Log-audit item 10: wrong peer sets (Auto -> FMCG peers, Utilities -> IT peers, retail -> generic set)."""

    @pytest.mark.parametrize("payload,expected", [
        # Yahoo's real sector / industry pairs
        ({"sector": "Consumer Cyclical", "industry": "Auto Manufacturers"}, "AUTO"),
        ({"sector": "Consumer Cyclical", "industry": "Auto Parts"}, "AUTO"),
        ({"sector": "Utilities", "industry": "Utilities - Renewable"}, "POWER"),
        ({"sector": "Utilities", "industry": "Utilities - Regulated Electric"}, "POWER"),
        ({"sector": "Consumer Cyclical", "industry": "Specialty Retail"}, "RETAIL"),
        ({"sector": "Consumer Cyclical", "industry": "Apparel Retail"}, "RETAIL"),
        ({"sector": "Consumer Defensive", "industry": "Packaged Foods"}, "FMCG"),
        ({"sector": "Consumer Defensive", "industry": "Household & Personal Products"}, "FMCG"),
        ({"sector": "Technology", "industry": "Information Technology Services"}, "IT"),
        ({"sector": "Technology", "industry": "Software - Application"}, "IT"),
        ({"sector": "Financial Services", "industry": "Banks - Regional"}, "BANK"),
        ({"sector": "Financial Services", "industry": "Capital Markets"}, "BANK"),   # industry unmapped -> sector
        ({"sector": "Healthcare", "industry": "Drug Manufacturers - General"}, "PHARMA"),
        ({"sector": "Healthcare", "industry": "Biotechnology"}, "PHARMA"),            # BIOTECH is not TECH
        ({"sector": "Basic Materials", "industry": "Steel"}, "METAL"),
        ({"sector": "Energy", "industry": "Oil & Gas Refining & Marketing"}, "ENERGY"),
        ({"sector": "Industrials", "industry": "Waste Management"}, "DEFAULT"),
    ])
    def test_real_yahoo_sector_industry_pairs(self, payload, expected):
        assert pmq.detect_sector(payload) == expected

    @pytest.mark.parametrize("text", [
        "Capital Goods", "Utilities", "Hospitality", "Capital Markets", "Digital Infrastructure",
    ])
    def test_it_is_not_matched_inside_other_words(self, text):
        assert pmq.detect_sector({"sector": text}) != "IT"

    @pytest.mark.parametrize("text", ["Consumer Cyclical", "Consumer Discretionary", "Consumer Durables"])
    def test_consumer_cyclical_is_not_fmcg(self, text):
        assert pmq.detect_sector({"sector": text}) == "DEFAULT"

    @pytest.mark.parametrize("text", ["Consumer", "Consumer Staples", "Consumer Defensive"])
    def test_consumer_staples_and_bare_consumer_stay_fmcg(self, text):
        assert pmq.detect_sector({"sector": text}) == "FMCG"

    def test_industry_beats_a_misleading_sector(self):
        assert pmq.detect_sector({"sector": "Consumer Defensive", "industry": "Auto Parts"}) == "AUTO"

    def test_sector_is_used_when_industry_names_nothing(self):
        assert pmq.detect_sector({"sector": "Technology", "industry": "Scientific & Technical Instruments".replace("Technical", "Gadgets")}) == "IT"

    def test_sectordisp_is_the_last_resort(self):
        assert pmq.detect_sector({"industry": "Misc", "sector": "", "sectorDisp": "Utilities"}) == "POWER"

    @pytest.mark.parametrize("bad", [None, 0, 12.5, ["Auto"], {}])
    def test_non_string_values_do_not_raise(self, bad):
        assert pmq.detect_sector({"sector": bad, "industry": bad, "sectorDisp": bad}) == "DEFAULT"

    def test_punctuation_separated_words_still_match(self):
        assert pmq.detect_sector({"industry": "Oil&Gas"}) == "ENERGY"
        assert pmq.detect_sector({"industry": "Auto-Components"}) == "AUTO"

    def test_automation_is_not_auto(self):
        assert pmq.detect_sector({"industry": "Industrial Automation"}) == "DEFAULT"

    def test_new_sectors_have_peer_sets_and_exclude_the_symbol_itself(self):
        for key in ("POWER", "RETAIL"):
            assert pmq.DEFAULT_PEERS[key] and all(p.endswith(".NS") for p in pmq.DEFAULT_PEERS[key])
        assert "NTPC.NS" not in pmq.build_peer_list("NTPC", "POWER")
        assert pmq.build_peer_list("CLEANMAX", "POWER")[0] == "NTPC.NS"
        assert pmq.build_peer_list("SSRETAIL", "RETAIL")[0] == "DMART.NS"
        assert "RELIANCE.NS" not in pmq.build_peer_list("SSRETAIL", "RETAIL")


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


    def test_worker_exception_maps_to_empty_and_others_survive(self, monkeypatch):
        # fetch_fundamentals normally swallows its own errors, but a raise escaping the
        # worker (e.g. a bug / BaseException-free error path) must be logged and mapped
        # to {} without losing the other symbols (lines 173-175).
        def _fake(url, sym, timeout):
            if sym == "BAD.NS":
                raise RuntimeError("worker blew up")
            return {"pe": 12}
        monkeypatch.setattr(pmq, "fetch_fundamentals", _fake)
        result = pmq.fetch_fundamentals_batch("http://mds/", ["BAD.NS", "OK.NS"])
        assert result == {"BAD.NS": {}, "OK.NS": {"pe": 12}}


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
        result = pmq.compute_peer_relative("X", {"pe_ratio": 10.0, "sector": "Basic Materials"}, "http://mds/")
        assert result["peer_score"] > 50.0

    def test_expensive_pe_lowers_score(self, monkeypatch):
        self._patch_batch(monkeypatch, {"pe_ratio": 10.0})
        result = pmq.compute_peer_relative("X", {"pe_ratio": 50.0, "sector": "Basic Materials"}, "http://mds/")
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


# ── _pick: primary key first, alias only when the primary is unusable ──────────

class TestPick:
    K = ("a", "b", "c")

    def test_primary_wins(self):
        assert pmq._pick({"a": 1, "b": 2}, self.K) == 1.0

    def test_zero_primary_is_a_real_value(self):
        assert pmq._pick({"a": 0, "b": 2}, self.K) == 0.0   # `a or b` returned 2

    def test_zero_primary_skipped_when_skip_zero(self):
        assert pmq._pick({"a": 0, "b": 2}, self.K, skip_zero=True) == 2.0

    def test_skip_zero_with_no_alias_returns_default(self):
        assert pmq._pick({"a": 0}, self.K, skip_zero=True) == 0.0
        assert pmq._pick({"a": 0}, self.K, skip_zero=True, default=7.0) == 7.0

    @pytest.mark.parametrize("bad", [None, "N/A", "", float("nan"), [], object()])
    def test_unusable_primary_falls_through(self, bad):
        assert pmq._pick({"a": bad, "b": 5}, self.K) == 5.0

    def test_falls_through_several_keys(self):
        assert pmq._pick({"a": None, "b": "x", "c": 3}, self.K) == 3.0

    def test_numeric_string_is_accepted(self):
        assert pmq._pick({"a": "12.5"}, self.K) == 12.5

    def test_negative_value_is_kept(self):
        assert pmq._pick({"a": -4, "b": 9}, self.K) == -4.0

    def test_nothing_usable_returns_default(self):
        assert pmq._pick({}, self.K) == 0.0
        assert pmq._pick({"a": None}, self.K, default=3.0) == 3.0

    @pytest.mark.parametrize("not_a_dict", [None, [], "abc", 5])
    def test_non_dict_returns_default(self, not_a_dict):
        assert pmq._pick(not_a_dict, self.K, default=9.0) == 9.0


class TestZeroPrimaryInPeerRelativeAndConsistency:
    def _patch(self, monkeypatch, fetched):
        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", lambda url, syms, timeout=15.0: fetched)

    def test_stock_zero_growth_and_roe_not_replaced_by_alias(self, monkeypatch):
        self._patch(monkeypatch, {})
        stock = {"roe": 0, "returnOnEquity": 15, "revenue_growth_yoy": 0, "revenueGrowth": 20,
                 "profit_growth_yoy": 0, "earningsGrowth": 30}
        out = pmq.compute_peer_relative("A", stock, "http://x", peers=["B"])
        assert out["stock"]["roe"] == 0.0
        assert out["stock"]["revenue_growth_yoy"] == 0.0
        assert out["stock"]["profit_growth_yoy"] == 0.0

    def test_stock_zero_pe_still_uses_alias(self, monkeypatch):
        self._patch(monkeypatch, {})
        out = pmq.compute_peer_relative("A", {"pe_ratio": 0, "trailingPE": 18}, "http://x", peers=["B"])
        assert out["stock"]["pe"] == 18.0

    def test_peer_zero_primary_not_replaced_by_alias(self, monkeypatch):
        self._patch(monkeypatch, {"B.NS": {"pe_ratio": 20, "roe": 0, "returnOnEquity": 15,
                                           "revenue_growth_yoy": 0, "revenueGrowth": 20,
                                           "profit_growth_yoy": 0, "earningsGrowth": 30}})
        out = pmq.compute_peer_relative("A", {"pe_ratio": 20}, "http://x", peers=["B"])
        d = out["peer_details"][0]
        assert (d["roe"], d["rev_g"], d["profit_g"]) == (0.0, 0.0, 0.0)
        assert out["peer_avg"]["roe"] == 0.0           # a 0 is not averaged in as "15"

    def test_peer_nan_primary_falls_through_to_alias(self, monkeypatch):
        self._patch(monkeypatch, {"B.NS": {"pe_ratio": 20, "roe": float("nan"), "returnOnEquity": 15}})
        d = pmq.compute_peer_relative("A", {"pe_ratio": 20}, "http://x", peers=["B"])["peer_details"][0]
        assert d["roe"] == 15.0

    def test_quarterly_zero_primary_not_replaced_by_alias(self):
        q = [{"period": "Q1", "revenue_growth": 0, "revenue_growth_yoy": 9,
              "profit_growth": 0, "net_income_growth": 8}]
        r = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert r["detail"][0]["revenue_growth"] == 0.0
        assert r["detail"][0]["profit_growth"] == 0.0
        assert r["positive_revenue_quarters"] == 0     # a flat quarter is not "positive"

    def test_quarterly_missing_primary_uses_aliases_in_order(self):
        q = [{"period": "Q1", "revenue_growth": None, "revenueGrowth": 6,
              "profit_growth": "n/a", "earningsGrowth": None, "net_income_growth": 4}]
        r = pmq.compute_multi_quarter_consistency(quarterly=q)
        assert (r["detail"][0]["revenue_growth"], r["detail"][0]["profit_growth"]) == (6.0, 4.0)

    def test_fundamentals_fallback_zero_primary_not_replaced_by_alias(self):
        # primary YoY 0 + alias 12: previously read 12 and produced a 1-quarter signal.
        fund = {"revenue_growth_yoy": 0, "revenueGrowth": 12, "profit_growth_yoy": 0, "earningsGrowth": 12}
        assert pmq.compute_multi_quarter_consistency(fundamentals=fund)["quarters_checked"] == 0

    def test_fundamentals_fallback_alias_used_when_primary_absent(self):
        fund = {"revenueGrowth": 12, "earningsGrowth": 7}
        r = pmq.compute_multi_quarter_consistency(fundamentals=fund)
        assert (r["avg_revenue_growth"], r["avg_profit_growth"]) == (12.0, 7.0)


# ── build_peer_list / compute_peer_relative peer-set handling ─────────────────

class TestBuildPeerList:
    def test_normalises_bare_symbols(self):
        assert pmq.build_peer_list("A", "IT", ["tcs", " infy "]) == ["TCS.NS", "INFY.NS"]

    def test_keeps_bo_suffix(self):
        assert pmq.build_peer_list("A", "IT", ["X.BO", "Y"]) == ["X.BO", "Y.NS"]

    def test_removes_duplicates_across_spellings(self):
        assert pmq.build_peer_list("A", "IT", ["B", "b", "B.NS", "C", "C"]) == ["B.NS", "C.NS"]

    def test_excludes_the_symbol_in_any_spelling(self):
        assert pmq.build_peer_list("tcs", "IT", ["TCS", "tcs.ns", "INFY"]) == ["INFY.NS"]

    def test_duplicates_do_not_consume_max_peers_slots(self):
        assert pmq.build_peer_list("A", "IT", ["B", "B", "B", "C", "D"], max_peers=2) == ["B.NS", "C.NS"]

    def test_self_does_not_consume_max_peers_slots(self):
        assert pmq.build_peer_list("A", "IT", ["A", "B", "C"], max_peers=2) == ["B.NS", "C.NS"]

    @pytest.mark.parametrize("n", [0, -1, -5])
    def test_zero_or_negative_max_peers_means_no_peers(self, n):
        # a negative max_peers used to slice from the END of the list ([:-1])
        assert pmq.build_peer_list("A", "IT", ["B", "C", "D"], max_peers=n) == []

    def test_default_limit_is_default_max_peers(self):
        many = [f"S{i}" for i in range(20)]
        assert len(pmq.build_peer_list("A", "IT", many)) == pmq.DEFAULT_MAX_PEERS == 5

    def test_none_and_empty_use_the_sector_default(self):
        assert pmq.build_peer_list("A", "BANK", None) == pmq.DEFAULT_PEERS["BANK"]
        assert pmq.build_peer_list("A", "BANK", []) == pmq.DEFAULT_PEERS["BANK"]

    def test_unknown_sector_uses_default_list(self):
        assert pmq.build_peer_list("A", "NOPE", None) == pmq.DEFAULT_PEERS["DEFAULT"]

    def test_default_list_containing_the_symbol_drops_it(self):
        out = pmq.build_peer_list("TCS", "IT", None)
        assert "TCS.NS" not in out and len(out) == len(pmq.DEFAULT_PEERS["IT"]) - 1

    def test_empty_symbol_is_normalised_not_crashing(self):
        assert pmq.build_peer_list("", "IT", ["B"]) == ["B.NS"]


class TestComputePeerRelativePeerSet:
    @staticmethod
    def _spy(monkeypatch, data=None):
        seen = []

        def fake(url, syms, timeout=15.0):
            seen.append(list(syms))
            return {s: (data or {}) for s in syms}

        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", fake)
        return seen

    def test_bare_peer_symbols_are_fetched_in_canonical_form(self, monkeypatch):
        seen = self._spy(monkeypatch, {"pe_ratio": 20, "roe": 10})
        out = pmq.compute_peer_relative("A", {"pe_ratio": 20}, "http://x", peers=["B", "C"])
        assert seen == [["B.NS", "C.NS"]]                      # was ["B", "C"] -> never found
        assert out["peers_used"] == ["B.NS", "C.NS"]

    def test_duplicate_spellings_are_counted_once_in_the_average(self, monkeypatch):
        seen = self._spy(monkeypatch, {"pe_ratio": 20})
        out = pmq.compute_peer_relative("A", {"pe_ratio": 20}, "http://x", peers=["B", "b", "B.NS", "C"])
        assert seen == [["B.NS", "C.NS"]]
        assert out["peers_used"] == ["B.NS", "C.NS"]

    def test_max_peers_zero_and_negative_use_no_peers(self, monkeypatch):
        for n in (0, -1):
            seen = self._spy(monkeypatch)
            out = pmq.compute_peer_relative("A", {"pe_ratio": 20}, "http://x", peers=["B", "C"], max_peers=n)
            assert seen == [[]] and out["peers_used"] == []
            assert out["peer_score"] == pytest.approx(50.0)

    def test_default_max_peers_is_five(self, monkeypatch):
        seen = self._spy(monkeypatch)
        pmq.compute_peer_relative("A", {}, "http://x", peers=[f"S{i}" for i in range(9)])
        assert seen == [[f"S{i}.NS" for i in range(5)]]

    def test_stock_listed_in_peers_in_a_different_spelling_is_excluded(self, monkeypatch):
        seen = self._spy(monkeypatch)
        pmq.compute_peer_relative("tcs", {}, "http://x", peers=["TCS.NS", "INFY"])
        assert seen == [["INFY.NS"]]

    def test_signature_default_matches_constant(self):
        import inspect
        assert inspect.signature(pmq.compute_peer_relative).parameters["max_peers"].default == pmq.DEFAULT_MAX_PEERS
