"""
tests/test_fundamental_main.py — coverage for fundamental/main.py

No network, no DB. httpx.get is faked, the sqlalchemy import inside
sector_typical_pe_from_db is replaced through sys.modules, and the peer batch
fetch / IndianAPI fallback / apply_to_analyze_response hooks are monkeypatched.
Route functions are called directly (FastAPI's @app.get returns the original
function), so no TestClient is needed.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_fundamental_main.py -v
"""
from __future__ import annotations

import logging
import os
import runpy
import sys
import types
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "fundamental"))

import pytest

import httpx
import main as fm  # noqa: E402  (fundamental/main.py)
import peer_multi_quarter as pmq  # noqa: E402  (fundamental/peer_multi_quarter.py)


# ── shared helpers ────────────────────────────────────────────────────────────

@pytest.fixture()
def rl(monkeypatch):
    """Stub rate_limit_report; records calls."""
    calls = []
    fake = types.ModuleType("rate_limit_report")
    fake.report_if_rate_limited = lambda exc, **kw: calls.append((exc, kw))
    monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
    return calls


class _Resp:
    def __init__(self, payload=None, raise_exc=None, bad_json=False):
        self._payload, self._raise, self._bad_json = payload, raise_exc, bad_json

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


def _status_error(code):
    return httpx.HTTPStatusError("boom", request=None, response=SimpleNamespace(status_code=code))


class _Env:
    """Wires every external hook of analyze() to a controllable fake."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.urls = []
        self.peer_calls = []
        self.payload = {}
        self.http_exc = None
        self.http_resp = None
        self.peer_rows = {}
        self.peer_exc = None
        self.mp.setattr(fm.httpx, "get", self._get)
        self.mp.setattr(fm, "get_fundamentals_with_fallback", None)
        self.mp.setattr(fm, "apply_to_analyze_response", None)
        self.mp.setattr(fm, "sector_typical_pe_from_db", lambda sector: 20)
        self.mp.setattr(pmq, "fetch_fundamentals_batch", self._batch)

    def _get(self, url, timeout=None):
        self.urls.append(url)
        if self.http_exc:
            raise self.http_exc
        return self.http_resp if self.http_resp is not None else _Resp(self.payload)

    def _batch(self, base_url, symbols):
        self.peer_calls.append((base_url, list(symbols)))
        if self.peer_exc:
            raise self.peer_exc
        return {s: self.peer_rows[s] for s in symbols if s in self.peer_rows}

    def analyze(self, f=None, symbol="TESTCO", force=False):
        if f is not None:
            self.payload = f
        return fm.analyze(symbol, force=force)


@pytest.fixture()
def env(monkeypatch, rl):
    return _Env(monkeypatch)


def _delta(env, fields, **kw):
    """Score change from the neutral 50 base for a set of raw fields."""
    return env.analyze(dict(fields), **kw)["fundamental_score"] - 50


# ── _pct ──────────────────────────────────────────────────────────────────────

class TestPct:
    def test_none(self):
        assert fm._pct(None) is None

    def test_fraction_scaled_to_percent(self):
        assert fm._pct(0.5) == 50.0

    def test_negative_fraction_scaled(self):
        assert fm._pct(-0.3) == pytest.approx(-30.0)

    def test_already_percent_left_alone(self):
        assert fm._pct(12) == 12
        assert fm._pct(5) == 5


# ── _normalize_debt_to_equity ─────────────────────────────────────────────────

class TestNormalizeDebtToEquity:
    def test_none(self):
        assert fm._normalize_debt_to_equity(None) is None

    def test_bad_string(self):
        assert fm._normalize_debt_to_equity("abc") is None

    def test_nan(self):
        assert fm._normalize_debt_to_equity(float("nan")) is None

    def test_small_value_unchanged_and_rounded(self):
        assert fm._normalize_debt_to_equity(0.9876) == 0.99

    def test_percent_style_scaled_for_non_financial(self):
        assert fm._normalize_debt_to_equity(95.4, "Technology") == 0.95

    def test_exactly_50_not_scaled(self):
        assert fm._normalize_debt_to_equity(50, "Technology") == 50

    def test_no_sector_treated_as_non_financial(self):
        assert fm._normalize_debt_to_equity(120) == 1.2

    @pytest.mark.parametrize("sector", ["Private Bank", "Financial Services", "Insurance"])
    def test_financials_only_scaled_above_200(self, sector):
        assert fm._normalize_debt_to_equity(150, sector) == 150
        assert fm._normalize_debt_to_equity(500, sector) == 5.0

    def test_string_number_accepted(self):
        assert fm._normalize_debt_to_equity("80", None) == 0.8


# ── sector_typical_pe_from_db ─────────────────────────────────────────────────

def _fake_sqlalchemy(monkeypatch, rows=None, exec_exc=None):
    seen = {}
    mod = types.ModuleType("sqlalchemy")

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            seen["query"], seen["params"] = query, params
            if exec_exc:
                raise exec_exc
            return SimpleNamespace(fetchall=lambda: list(rows or []))

    class Engine:
        def connect(self):
            return Conn()

    def create_engine(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return Engine()

    mod.create_engine = create_engine
    mod.text = lambda s: s
    monkeypatch.setitem(sys.modules, "sqlalchemy", mod)
    return seen


@pytest.fixture()
def no_db_env(monkeypatch):
    monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


class TestSectorTypicalPeFromDb:
    def test_empty_sector_uses_default(self, no_db_env):
        assert fm.sector_typical_pe_from_db("") == float(fm.DEFAULT_TYPICAL_PE)
        assert fm.sector_typical_pe_from_db(None) == float(fm.DEFAULT_TYPICAL_PE)

    def test_no_db_url_falls_back_to_static(self, no_db_env):
        assert fm.sector_typical_pe_from_db("Technology") == 26.0

    def test_unknown_sector_uses_default(self, no_db_env):
        assert fm.sector_typical_pe_from_db("Basket Weaving") == float(fm.DEFAULT_TYPICAL_PE)

    def test_db_median_used_when_enough_samples(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        rows = [(v,) for v in (10, 12, 14, 16, 18, 20, 22, 24)]
        seen = _fake_sqlalchemy(monkeypatch, rows)
        assert fm.sector_typical_pe_from_db("Technology") == 17.0
        assert seen["params"] == {"sector": "Technology"}
        assert seen["url"] == "postgresql://u:p@h/db"
        assert seen["kw"]["pool_size"] == 1 and seen["kw"]["connect_args"] == {"connect_timeout": 5}

    def test_cache_url_preferred_and_postgres_scheme_rewritten(self, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgres://cache/db")
        monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")
        seen = _fake_sqlalchemy(monkeypatch, [(20,)] * 8)
        fm.sector_typical_pe_from_db("IT")
        assert seen["url"] == "postgresql://cache/db"

    def test_non_positive_and_null_rows_ignored(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        rows = [(None,), (0,), (-5,)] + [(v,) for v in (10, 12, 14, 16, 18, 20, 22, 24)]
        _fake_sqlalchemy(monkeypatch, rows)
        assert fm.sector_typical_pe_from_db("Technology") == 17.0

    def test_too_few_samples_falls_back(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        _fake_sqlalchemy(monkeypatch, [(10,)] * 7)
        assert fm.sector_typical_pe_from_db("Technology") == 26.0

    def test_query_error_falls_back(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        monkeypatch.delenv("CACHE_DATABASE_URL", raising=False)
        _fake_sqlalchemy(monkeypatch, exec_exc=RuntimeError("db down"))
        assert fm.sector_typical_pe_from_db("Energy") == 11.0


# ── _sector_relative_pe_score ─────────────────────────────────────────────────

class TestSectorRelativePeScore:
    @pytest.fixture(autouse=True)
    def _typical(self, monkeypatch):
        monkeypatch.setattr(fm, "sector_typical_pe_from_db", lambda sector: 20)

    @pytest.mark.parametrize("pe", [None, 0, -5])
    def test_no_signal_for_missing_or_non_positive(self, pe):
        assert fm._sector_relative_pe_score(pe, "Technology") == (0, None)

    @pytest.mark.parametrize("pe,delta,phrase", [
        (10, 8, "well below"),
        (14, 4, "is below the"),      # ratio 0.70 exactly -> not "well below"
        (16, 4, "is below the"),
        (18, 0, "in line with"),      # ratio 0.90 exactly -> in line
        (23, 0, "in line with"),      # ratio 1.15 exactly -> in line
        (25, -4, "is above the"),
        (30, -4, "is above the"),     # ratio 1.50 exactly -> still "above"
        (40, -8, "well above"),
    ])
    def test_band_boundaries(self, pe, delta, phrase):
        got_delta, reason = fm._sector_relative_pe_score(pe, "Technology")
        assert got_delta == delta
        assert phrase in reason
        assert "P/E at %.1f" % pe in reason

    def test_sector_label_and_typical_in_reason(self):
        _, reason = fm._sector_relative_pe_score(20, "Technology")
        assert "Technology typical range (~20)" in reason

    def test_missing_sector_labelled_broad_market(self):
        _, reason = fm._sector_relative_pe_score(20, None)
        assert "broad market" in reason


# ── _multi_quarter_consistency ────────────────────────────────────────────────

class TestMultiQuarterConsistency:
    @pytest.mark.parametrize("data", [None, [], [5]])
    def test_too_little_data_is_neutral(self, data):
        assert fm._multi_quarter_consistency(data) == (50.0, False)

    def test_steady_growth_scores_high_and_capped(self):
        assert fm._multi_quarter_consistency([10, 8, 6, 4]) == (100.0, True)

    def test_steady_decline_scores_low(self):
        score, ok = fm._multi_quarter_consistency([4, 6, 8, 10])
        assert ok is False
        assert score == pytest.approx(21.6, abs=0.05)

    def test_mostly_up_one_down(self):
        score, ok = fm._multi_quarter_consistency([10, 8, 9, 7])
        assert ok is True
        assert score == pytest.approx(77.7, abs=0.05)

    def test_two_up_two_down_is_62_band(self):
        # newest-first [5,4,5,4,5]: pcts +0.25, -0.2, +0.25, -0.2 -> pos 2, neg 2
        score, ok = fm._multi_quarter_consistency([5, 4, 5, 4, 5])
        assert ok is True
        assert 60 <= score <= 68

    def test_single_up_single_down_is_48_band(self):
        score, ok = fm._multi_quarter_consistency([10, 8, 9])
        assert ok is False
        assert 40 <= score <= 60

    def test_flat_quarters_score_32_band(self):
        score, ok = fm._multi_quarter_consistency([10, 10, 10])
        assert (score, ok) == (32.0, False)

    def test_zero_older_value_guarded(self):
        assert fm._multi_quarter_consistency([5, 0]) == (32.0, False)

    def test_dict_inputs_and_key_priority(self):
        score, ok = fm._multi_quarter_consistency([{"eps": 2}, {"eps": 1}])
        assert (score, ok) == (60.0, False)

    def test_dict_falls_through_to_later_keys(self):
        data = [{"eps": None, "revenue": 200}, {"revenue": 100}, {"revenue": 50}]
        assert fm._multi_quarter_consistency(data)[1] is True

    def test_dict_with_unparseable_value_skipped(self):
        data = [{"eps": "x", "earnings": 3}, {"eps": 2}]
        assert fm._multi_quarter_consistency(data)[0] > 50

    def test_numeric_strings_accepted_junk_skipped(self):
        assert fm._multi_quarter_consistency(["3", "abc", "2", None])[0] != 50.0

    def test_all_junk_returns_neutral(self):
        assert fm._multi_quarter_consistency(["a", None, {}]) == (50.0, False)

    def test_only_first_six_used(self):
        # 6 rising quarters followed by a 7th value that would flip the verdict
        # if it were used (6 -> 1000 reads as a crash); it must be ignored.
        data = [16, 14, 12, 10, 8, 6, 1000]
        assert fm._multi_quarter_consistency(data) == (96.7, True)


# ── routes: root / health ─────────────────────────────────────────────────────

class TestRootAndHealth:
    def test_root(self):
        r = fm.root()
        assert r["service"] == "Stockky Fundamental Analysis Service"
        assert r["status"] == "running"
        assert "/analyze/{symbol}" in r["endpoints"]

    def test_health_flags(self, monkeypatch):
        monkeypatch.setenv("INDIANAPI_KEY", "k")
        monkeypatch.setattr(fm, "get_fundamentals_with_fallback", lambda *a: None)
        h = fm.health()
        assert h["status"] == "ok"
        assert h["indianapi_configured"] is True
        assert h["indianapi_module"] is True

    def test_health_without_key_or_module(self, monkeypatch):
        monkeypatch.delenv("INDIANAPI_KEY", raising=False)
        monkeypatch.setattr(fm, "get_fundamentals_with_fallback", None)
        h = fm.health()
        assert h["indianapi_configured"] is False
        assert h["indianapi_module"] is False


# ── analyze: fetching, errors and fallbacks ───────────────────────────────────

class TestAnalyzeFetching:
    def test_url_and_force_flag(self, env):
        env.analyze({"roe": 25}, symbol="INFY.NS", force=True)
        assert env.urls[-1] == f"{fm.MARKET_DATA_URL}/fundamentals/INFY.NS?force=true"
        env.analyze({"roe": 25}, symbol="INFY.NS", force=False)
        assert env.urls[-1].endswith("?force=false")

    # ── group112 (item 4 leftover): bare tickers are requested as .NS, everything else unchanged ──
    @pytest.mark.parametrize("given, requested", [
        ("INFY", "INFY.NS"), ("infy", "INFY.NS"), ("  tcs ", "TCS.NS"), ("M&M", "M&M.NS"), ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"),
        ("360ONE", "360ONE.NS"),
        ("INFY.NS", "INFY.NS"), ("RELIANCE.BO", "RELIANCE.BO"), ("infy.ns", "infy.ns"),
        ("NIFTY", "NIFTY"), ("NIFTY50", "NIFTY50"), ("NIFTYBANK", "NIFTYBANK"), ("BANKNIFTY", "BANKNIFTY"),
        ("SENSEX", "SENSEX"), ("INDIAVIX", "INDIAVIX"), ("^NSEI", "^NSEI"),
        ("NIFTY BANK", "NIFTY BANK"), ("KFIN TECHNOLOGIES", "KFIN TECHNOLOGIES"),
    ])
    def test_market_data_url_uses_the_canonical_spelling_only_for_plain_tickers(self, env, given, requested):
        env.analyze({"roe": 25}, symbol=given)
        assert env.urls[-1] == f"{fm.MARKET_DATA_URL}/fundamentals/{requested}?force=false"

    def test_both_spellings_of_one_company_make_the_same_request(self, env):
        env.analyze({"roe": 25}, symbol="INFY")
        env.analyze({"roe": 25}, symbol="INFY.NS")
        assert env.urls[-2] == env.urls[-1]

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_symbol_is_passed_through_unchanged(self, blank):
        assert fm._md_fundamentals_symbol(blank) == blank

    def test_symbol_uppercased_in_result(self, env):
        assert env.analyze({"roe": 25}, symbol="infy")["symbol"] == "INFY"

    def test_non_dict_body_treated_as_empty(self, env):
        env.http_resp = _Resp([1, 2, 3])
        out = env.analyze()
        assert out["raw"] == {} and out["fallback_used"] is True

    def test_empty_body_treated_as_empty(self, env):
        out = env.analyze({})
        assert out["fallback_used"] is True
        assert any("Live data temporarily unavailable" in r for r in out["reasons"])

    def test_timeout_falls_back(self, env, caplog):
        env.http_exc = httpx.TimeoutException("slow")
        with caplog.at_level(logging.WARNING):
            out = env.analyze()
        assert out["fallback_used"] is True and out["fundamental_score"] == 50
        assert "timed out" in caplog.text

    def test_generic_exception_falls_back(self, env):
        env.http_exc = RuntimeError("connection reset")
        assert env.analyze()["fallback_used"] is True

    def test_bad_json_falls_back(self, env):
        env.http_resp = _Resp(bad_json=True)
        assert env.analyze()["fallback_used"] is True

    @pytest.mark.parametrize("code", [500, 502, 429])
    def test_retryable_status_falls_back(self, env, code):
        env.http_exc = _status_error(code)
        assert env.analyze()["fallback_used"] is True

    @pytest.mark.parametrize("code", [400, 404])
    def test_client_error_status_raises_http_exception(self, env, code):
        env.http_exc = _status_error(code)
        with pytest.raises(fm.HTTPException) as ei:
            env.analyze()
        assert ei.value.status_code == code
        assert "Market data service error" in str(ei.value.detail)

    def test_status_error_reported_with_provider(self, env, rl):
        env.http_exc = _status_error(429)
        env.analyze(symbol="TCS")
        exc, kw = rl[0]
        assert exc is env.http_exc
        # group112: the reported path is the URL actually requested (canonical .NS spelling)
        assert kw == {"provider": "analysis", "path": "/fundamentals/TCS.NS", "symbol": "TCS", "status": 429}

    def test_5xx_reported_as_market_data_provider(self, env, rl):
        env.http_exc = _status_error(503)
        env.analyze(symbol="TCS")
        assert rl[0][1]["provider"] == "market_data"

    def test_report_failure_swallowed(self, env, monkeypatch):
        fake = types.ModuleType("rate_limit_report")

        def boom(*a, **k):
            raise RuntimeError("kv down")

        fake.report_if_rate_limited = boom
        monkeypatch.setitem(sys.modules, "rate_limit_report", fake)
        env.http_exc = _status_error(500)
        assert env.analyze()["fallback_used"] is True


class TestIndianApiFallback:
    def test_not_called_when_core_data_present(self, env):
        called = []
        env.mp.setattr(fm, "get_fundamentals_with_fallback", lambda *a: called.append(a))
        env.analyze({"pe_ratio": 15})
        assert called == []

    def test_fills_missing_fields_and_clears_fallback_flag(self, env):
        seen = {}

        def fake(symbol, yahoo_fn):
            seen["symbol"], seen["yahoo_none"] = symbol, yahoo_fn("X") is None
            return {"pe_ratio": 10, "roe": 25, "sector": "Technology", "industry": None}

        env.mp.setattr(fm, "get_fundamentals_with_fallback", fake)
        out = env.analyze({}, symbol="INFY")
        assert seen == {"symbol": "INFY", "yahoo_none": True}
        assert out["raw"]["roe"] == 25 and out["raw"]["sector"] == "Technology"
        assert "industry" not in out["raw"]          # None values are not merged
        assert out["fallback_used"] is False         # data now present

    def test_does_not_overwrite_existing_values(self, env):
        env.mp.setattr(fm, "get_fundamentals_with_fallback", lambda s, y: {"industry": "IT", "foo": 1})
        out = env.analyze({"industry": "Software"})
        assert out["raw"]["industry"] == "Software"   # existing non-core value kept
        assert out["raw"]["foo"] == 1

    def test_empty_result_ignored(self, env):
        env.mp.setattr(fm, "get_fundamentals_with_fallback", lambda s, y: None)
        assert env.analyze({})["fallback_used"] is True

    def test_exception_swallowed(self, env, caplog):
        def boom(s, y):
            raise RuntimeError("ia down")

        env.mp.setattr(fm, "get_fundamentals_with_fallback", boom)
        with caplog.at_level(logging.WARNING):
            out = env.analyze({})
        assert out["fallback_used"] is True
        assert "IndianAPI fallback failed" in caplog.text


# ── analyze: scoring table (each case is a delta from the neutral 50) ─────────

SCORING_CASES = [
    # valuation extras
    ({"pe_growth": 0.5}, 6, "PEG at 0.50 (<1.0)"),
    ({"pe_growth": 1.5}, 2, "reasonably valued"),
    ({"pe_growth": 3}, -2, "overvalued relative to growth"),
    ({"ev_ebitda": 8}, 4, "attractively valued"),
    ({"ev_ebitda": 25}, -4, "richly valued"),
    ({"price_to_book": 1}, 3, "low relative to book"),
    ({"price_to_book": 6}, -3, "high relative to book"),
    # growth
    ({"revenue_growth": 20}, 12, "strong expansion"),
    ({"revenue_growth": 10}, 5, "steady growth"),
    ({"revenue_growth": 2}, 0, "growth flat"),
    ({"revenue_growth": -5}, -12, "red flag"),
    ({"earnings_growth": 20}, 12, "profitable expansion"),
    ({"earnings_growth": -5}, -12, "margin or demand pressure"),
    # returns
    ({"roe": 25}, 10, "excellent capital efficiency"),
    ({"roe": 15}, 5, "healthy capital efficiency"),
    ({"roe": 5}, -8, "weak returns on equity"),
    ({"roce": 25}, 8, "excellent return on capital"),
    ({"roce": 15}, 4, "healthy capital efficiency"),
    ({"roce": 5}, -4, "weak return on capital"),
    ({"profit_margins": 20}, 8, "strong pricing power"),
    ({"profit_margins": 3}, -8, "thin profitability"),
    ({"opm": 25}, 6, "strong operational efficiency"),
    ({"opm": 5}, -6, "low operational efficiency"),
    # solvency / liquidity
    ({"debt_to_equity": 0.3}, 8, "low leverage"),
    ({"debt_to_equity": 1.0}, 0, "moderate leverage"),
    ({"debt_to_equity": 2.0}, -12, "high leverage"),
    ({"current_ratio": 2}, 4, "good short-term liquidity"),
    ({"current_ratio": 0.5}, -6, "poor short-term liquidity"),
    ({"interest_coverage": 5}, 5, "healthy ability to service debt"),
    ({"interest_coverage": 1}, -8, "risky debt servicing"),
    # ownership
    ({"held_percent_institutions": 45}, 6, "smart-money confidence"),
    ({"promoter_holding": 60}, 4, "strong management conviction"),
    ({"promoter_holding": 20}, -4, "low management ownership"),
    ({"promoter_pledging": 25}, -10, "high risk of margin calls"),
    ({"promoter_pledging": 15}, -5, "moderate risk"),
]


class TestScoring:
    @pytest.mark.parametrize("fields,delta,phrase", SCORING_CASES)
    def test_metric_delta(self, env, fields, delta, phrase):
        out = env.analyze(dict(fields))
        assert out["fundamental_score"] - 50 == delta
        assert any(phrase in r for r in out["reasons"]), out["reasons"]

    def test_neutral_metric_adds_default_reason(self, env):
        out = env.analyze({"roe": 10})      # ROE 8-12 -> no delta and no reason
        assert out["fundamental_score"] == 50
        assert out["reasons"] == ["Fundamental data partially available; score is based on available metrics"]

    # -- valuation ---------------------------------------------------------
    def test_negative_pe(self, env):
        out = env.analyze({"pe_ratio": -5})
        assert out["fundamental_score"] == 40
        assert out["valuation"] == "unprofitable (negative P/E)"
        assert "Negative P/E — company currently unprofitable" in out["reasons"]

    def test_cheap_pe_is_attractive(self, env):
        out = env.analyze({"pe_ratio": 10})       # typical patched to 20 -> +8
        assert out["valuation"] == "attractive"
        assert out["fundamental_score"] == 58

    def test_in_line_pe_is_fair(self, env):
        out = env.analyze({"pe_ratio": 20})
        assert out["valuation"] == "fair" and out["fundamental_score"] == 50

    def test_moderately_cheap_pe_still_fair_label(self, env):
        out = env.analyze({"pe_ratio": 16})       # +4 is not > 4
        assert out["valuation"] == "fair" and out["fundamental_score"] == 54

    def test_expensive_pe(self, env):
        out = env.analyze({"pe_ratio": 40})
        assert out["valuation"] == "expensive" and out["fundamental_score"] == 42

    def test_moderately_expensive_pe_still_fair_label(self, env):
        assert env.analyze({"pe_ratio": 25})["valuation"] == "fair"

    def test_forward_pe_bonus(self, env):
        out = env.analyze({"pe_ratio": 20, "forward_pe": 15})
        assert out["fundamental_score"] == 54
        assert any("Forward P/E lower" in r for r in out["reasons"])

    def test_forward_pe_higher_no_bonus(self, env):
        assert env.analyze({"pe_ratio": 20, "forward_pe": 25})["fundamental_score"] == 50

    def test_sector_passed_to_pe_scoring(self, env):
        seen = []
        env.mp.setattr(fm, "sector_typical_pe_from_db", lambda s: (seen.append(s), 20)[1])
        env.analyze({"pe_ratio": 20, "sector": "Technology"})
        assert seen == ["Technology"]

    # -- free cash flow ----------------------------------------------------
    @pytest.mark.parametrize("fcf,mcap,delta,yield_pct,phrase", [
        (10, 100, 10, 10.0, "strong cash generation"),
        (3, 100, 5, 3.0, "positive but modest"),
        (-5, 100, -10, -5.0, "burning cash"),
    ])
    def test_fcf_yield_bands(self, env, fcf, mcap, delta, yield_pct, phrase):
        out = env.analyze({"free_cashflow": fcf, "market_cap": mcap})
        assert out["fundamental_score"] - 50 == delta
        assert out["metrics"]["fcf_yield"] == yield_pct
        assert any(phrase in r for r in out["reasons"])

    def test_zero_fcf_yield_counts_as_burning(self, env):
        out = env.analyze({"free_cashflow": 0, "market_cap": 100})
        assert out["fundamental_score"] == 40

    def test_positive_fcf_without_market_cap(self, env):
        out = env.analyze({"free_cashflow": 50})
        assert out["fundamental_score"] == 58
        assert "fcf_yield" not in out["metrics"]

    def test_negative_fcf_without_market_cap(self, env):
        out = env.analyze({"free_cashflow": -50})
        assert out["fundamental_score"] == 40
        assert any("relies on external financing" in r for r in out["reasons"])

    # -- earnings yield ----------------------------------------------------
    def test_high_earnings_yield_for_moderate_growth(self, env):
        # pe 10 -> +8 (sector-relative) ; revenue 10 -> +5 ; earnings yield 10% -> +6
        out = env.analyze({"pe_ratio": 10, "revenue_growth": 10})
        assert out["fundamental_score"] == 69
        assert out["metrics"]["earnings_yield"] == 10.0

    def test_low_earnings_yield_for_moderate_growth(self, env):
        # pe 40 -> -8 ; revenue 10 -> +5 ; earnings yield 2.5% -> -4
        out = env.analyze({"pe_ratio": 40, "revenue_growth": 10})
        assert out["fundamental_score"] == 43
        assert out["metrics"]["earnings_yield"] == 2.5

    def test_earnings_yield_ignored_when_growth_not_moderate(self, env):
        out = env.analyze({"pe_ratio": 10, "revenue_growth": 20})    # 8 + 12, no yield adj
        assert out["fundamental_score"] == 70
        assert out["metrics"]["earnings_yield"] == 10.0

    def test_earnings_yield_not_computed_for_negative_pe(self, env):
        assert "earnings_yield" not in env.analyze({"pe_ratio": -3})["metrics"]

    # -- normalisation / metrics ------------------------------------------
    def test_debt_to_equity_normalised_before_scoring(self, env):
        out = env.analyze({"debt_to_equity": 95, "sector": "Technology"})   # Yahoo percent style
        assert out["metrics"]["debt_to_equity"] == 0.95
        assert out["fundamental_score"] == 50                               # moderate leverage
        assert any("moderate leverage" in r for r in out["reasons"])

    def test_debt_at_or_below_50_is_taken_as_an_already_normalised_multiple(self, env):
        # Input contract: market-data-service now converts Yahoo's percent debtToEquity to a
        # multiple at the source (yahoo_percent=True), so a value here is a multiple unless it
        # is large enough (>50) to be a leftover percent from older cached data. Values <=50 are
        # therefore NOT rescaled; rescaling here would double-divide fresh data (0.3 -> 0.003).
        out = env.analyze({"debt_to_equity": 30, "sector": "Technology"})
        assert out["metrics"]["debt_to_equity"] == 30
        assert out["fundamental_score"] == 38

    def test_metrics_dict_maps_institutional_holding(self, env):
        out = env.analyze({"held_percent_institutions": 12, "roe": 25})
        assert out["metrics"]["institutional_holding"] == 12
        assert out["metrics"]["roe"] == 25
        assert out["metrics"]["promoter_pledging"] is None

    def test_score_clamped_to_100(self, env):
        best = {
            "pe_ratio": 10, "forward_pe": 8, "pe_growth": 0.5, "ev_ebitda": 8, "price_to_book": 1,
            "revenue_growth": 20, "earnings_growth": 20, "roe": 25, "roce": 25,
            "profit_margins": 20, "opm": 25, "debt_to_equity": 0.3, "current_ratio": 2,
            "interest_coverage": 5, "free_cashflow": 10, "market_cap": 100,
            "held_percent_institutions": 45, "promoter_holding": 60,
        }
        assert env.analyze(best)["fundamental_score"] == 100

    def test_score_clamped_to_0(self, env):
        worst = {
            "pe_ratio": -5, "pe_growth": 3, "ev_ebitda": 25, "price_to_book": 6,
            "revenue_growth": -5, "earnings_growth": -5, "roe": 5, "roce": 5,
            "profit_margins": 3, "opm": 5, "debt_to_equity": 2.0, "current_ratio": 0.5,
            "interest_coverage": 1, "free_cashflow": -50, "promoter_holding": 20,
            "promoter_pledging": 25,
        }
        assert env.analyze(worst)["fundamental_score"] == 0


# ── analyze: multi-quarter consistency ────────────────────────────────────────

class TestAnalyzeMultiQuarter:
    def test_quarterly_list_used_and_bonus_applied(self, env):
        out = env.analyze({"roe": 10, "quarterly_earnings": [10, 8, 6, 4]})
        assert out["multi_quarter_score"] == 100.0 and out["multi_quarter_ok"] is True
        assert out["fundamental_score"] == 54                       # +4 consistency bonus
        assert out["quality_score"] == 77                           # round((54 + 100) / 2)
        assert out["multi_quarter_detail"] == {"score": 100.0, "ok": True, "quarters_used": 4}

    def test_alternate_key_earnings_quarters(self, env):
        out = env.analyze({"roe": 10, "earnings_quarters": [10, 8, 6]})
        assert out["multi_quarter_detail"]["quarters_used"] == 3

    def test_inconsistent_quarters_no_bonus(self, env):
        out = env.analyze({"roe": 10, "quarterly_earnings": [4, 6, 8, 10]})
        assert out["multi_quarter_ok"] is False
        assert out["fundamental_score"] == 50

    def test_proxy_when_both_growths_positive(self, env):
        out = env.analyze({"revenue_growth": 1, "earnings_growth": 1})
        assert out["multi_quarter_score"] == 70.0 and out["multi_quarter_ok"] is True
        assert any("multi-quarter proxy" in r for r in out["reasons"])
        assert out["multi_quarter_detail"]["quarters_used"] == 0

    def test_proxy_when_only_one_growth_positive(self, env):
        out = env.analyze({"revenue_growth": 1, "earnings_growth": -1})
        assert out["multi_quarter_score"] == 55.0 and out["multi_quarter_ok"] is False

    def test_short_list_falls_to_proxy(self, env):
        out = env.analyze({"quarterly_earnings": [5], "revenue_growth": 1, "earnings_growth": 1})
        assert out["multi_quarter_score"] == 70.0

    def test_no_data_stays_neutral(self, env):
        out = env.analyze({"roe": 25})
        assert out["multi_quarter_score"] == 50.0 and out["multi_quarter_ok"] is False

    def test_helper_exception_is_swallowed(self, env, caplog):
        def boom(_):
            raise RuntimeError("bad quarters")

        env.mp.setattr(fm, "_multi_quarter_consistency", boom)
        with caplog.at_level(logging.WARNING):
            out = env.analyze({"roe": 10, "quarterly_earnings": [1, 2, 3]})
        assert out["fundamental_score"] == 50
        assert "multi-quarter check failed" in caplog.text


# ── analyze: peer-relative section ────────────────────────────────────────────

class TestAnalyzePeers:
    def test_peer_list_for_known_symbol_excludes_self(self, env):
        out = env.analyze({"roe": 10}, symbol="INFY")
        assert out["peer_list"] == fm.peers_for("INFY", None)
        assert "INFY" not in out["peer_list"] and out["peer_list"]
        assert out["sector_normalized"] == "IT"

    def test_only_first_four_peers_fetched(self, env):
        out = env.analyze({"roe": 10}, symbol="INFY")
        base_url, requested = env.peer_calls[0]
        assert base_url == fm.MARKET_DATA_URL
        assert requested == out["peer_list"][:4] and len(requested) == 4

    def test_peer_rows_adjust_score_and_add_reason(self, env):
        env.peer_rows = {p: {"pe_ratio": 40, "roe": 10} for p in fm.peers_for("INFY", None)}
        base = {"pe_ratio": 10, "roe": 30}
        out = env.analyze(dict(base), symbol="INFY")
        rows = list(env.peer_rows.values())[:4]
        expected = fm.peer_relative_score(out["metrics"], fm.average_metrics(rows))
        assert out["peer_relative"] == expected
        assert out["peer_relative_score"] == expected["score"]
        assert expected["score"] > 50
        assert any(f"Peer-relative score {expected['score']} vs IT (4 peers)" in r for r in out["reasons"])
        # peers push a cheap, high-ROE stock up relative to the no-peer run
        env.peer_rows = {}
        no_peers = env.analyze(dict(base), symbol="INFY")
        assert out["fundamental_score"] > no_peers["fundamental_score"]

    def test_no_peer_data_note(self, env):
        out = env.analyze({"roe": 10}, symbol="INFY")
        assert out["peer_relative"] == {"score": 50.0, "components": {}, "note": "no_peer_data"}
        assert out["peer_relative_score"] == 50.0

    def test_unknown_symbol_without_sector_has_no_peers(self, env):
        out = env.analyze({"roe": 10}, symbol="ZZZCO")
        assert out["peer_list"] == []
        assert env.peer_calls[0][1] == []
        assert out["peer_relative"]["note"] == "no_peer_data"
        assert out["sector_normalized"] is None

    def test_sector_string_gives_peers_for_unknown_symbol(self, env):
        out = env.analyze({"roe": 10, "sector": "Pharmaceuticals"}, symbol="ZZZCO")
        assert out["sector_normalized"] == "Pharma"
        assert out["peer_list"] == fm.peers_for("ZZZCO", "Pharma") != []

    def test_it_sector_gives_peers_for_unknown_symbol(self, env):
        # analyze() feeds the *normalised* sector back into peers_for(), so
        # normalize_sector must be idempotent: "IT" -> "IT". A symbol outside
        # SYMBOL_SECTOR with a Yahoo "Software" sector still gets IT peers.
        out = env.analyze({"roe": 10, "sector": "Software - Application"}, symbol="ZZZCO")
        assert out["sector_normalized"] == "IT"
        assert out["peer_list"] == fm.peers_for("ZZZCO", "IT") != []

    @pytest.mark.parametrize("yahoo_sector, canonical", [
        ("Software - Application", "IT"),
        ("Credit Services", "Finance"),
        ("Engineering & Construction", "Infra"),
        ("Electrical Equipment", "Capital Goods"),
    ])
    def test_every_normalised_sector_gets_peers_for_unknown_symbol(self, env, yahoo_sector, canonical):
        out = env.analyze({"roe": 10, "sector": yahoo_sector}, symbol="ZZZCO")
        assert out["sector_normalized"] == canonical
        assert out["peer_list"] == fm.peers_for("ZZZCO", canonical) != []

    def test_batch_failure_leaves_default_note(self, env, caplog):
        env.peer_exc = RuntimeError("market data down")
        with caplog.at_level(logging.WARNING):
            out = env.analyze({"roe": 10}, symbol="INFY")
        assert out["peer_relative"]["note"] == "peers_not_fetched"
        assert out["peer_relative_score"] == 50.0
        assert "peer relative failed" in caplog.text


# ── analyze: result shape, notes, and the integration hook ────────────────────

class TestAnalyzeResult:
    def test_result_shape(self, env):
        out = env.analyze({"roe": 25, "sector": "Technology", "industry": "Software", "market_cap": 1000})
        for key in ("symbol", "fundamental_score", "valuation", "sector", "market_cap",
                    "peer_relative_score", "peer_relative", "peer_list", "sector_normalized",
                    "multi_quarter_score", "multi_quarter_ok", "multi_quarter_detail",
                    "quality_score", "industry", "reasons", "metrics", "raw", "fallback_used"):
            assert key in out
        assert out["sector"] == "Technology" and out["industry"] == "Software"
        assert out["market_cap"] == 1000
        assert out["raw"]["roe"] == 25

    def test_quality_score_is_average_of_score_and_multi_quarter(self, env):
        out = env.analyze({"roe": 25})                  # score 60, multi-q neutral 50
        assert out["fundamental_score"] == 60 and out["quality_score"] == 55

    def test_stale_flag_adds_reason(self, env):
        out = env.analyze({"roe": 25, "stale": True})
        assert any("Yahoo Finance rate limit" in r for r in out["reasons"])

    def test_fallback_reason_when_no_data(self, env):
        out = env.analyze({})
        assert out["fallback_used"] is True
        assert "Live data temporarily unavailable — score is based on last known or default values" in out["reasons"]
        assert out["fundamental_score"] == 50

    def test_fallback_not_flagged_when_only_secondary_metrics_present(self, env):
        assert env.analyze({"debt_to_equity": 1.0})["fallback_used"] is False

    def test_fallback_flagged_when_only_unlisted_metrics_present(self, env):
        assert env.analyze({"opm": 25})["fallback_used"] is True

    def test_apply_hook_result_returned(self, env):
        seen = {}

        def hook(symbol, analyze_payload, market_data_url):
            seen.update(symbol=symbol, url=market_data_url, score=analyze_payload["fundamental_score"])
            return {**analyze_payload, "unified": True}

        env.mp.setattr(fm, "apply_to_analyze_response", hook)
        out = env.analyze({"roe": 25}, symbol="INFY")
        assert out["unified"] is True
        assert seen == {"symbol": "INFY", "url": fm.MARKET_DATA_URL, "score": 60}

    def test_apply_hook_failure_keeps_original(self, env, caplog):
        def boom(**kw):
            raise RuntimeError("hook broke")

        env.mp.setattr(fm, "apply_to_analyze_response", boom)
        with caplog.at_level(logging.WARNING):
            out = env.analyze({"roe": 25}, symbol="INFY")
        assert out["fundamental_score"] == 60 and "unified" not in out
        assert "apply_to_analyze_response failed" in caplog.text


# ── import fallback + __main__ block ──────────────────────────────────────────

_FUND_MAIN = os.path.join(os.path.dirname(_HERE), "fundamental", "main.py")


class TestModuleLevel:
    def test_wire_peer_multi_quarter_missing_disables_hook(self, monkeypatch):
        # `from wire_peer_multi_quarter import ...` failing must leave the hook as None
        # instead of breaking service start-up (lines 8-9).
        monkeypatch.setitem(sys.modules, "wire_peer_multi_quarter", None)
        ns = runpy.run_path(_FUND_MAIN, run_name="fund_main_no_wire")
        assert ns["apply_to_analyze_response"] is None

    def test_wire_peer_multi_quarter_present_by_default(self):
        assert callable(fm.apply_to_analyze_response)

    def _run_main(self, monkeypatch):
        started = []
        fake = types.ModuleType("uvicorn")
        fake.run = lambda *a, **k: started.append((a, k))
        monkeypatch.setitem(sys.modules, "uvicorn", fake)
        runpy.run_path(_FUND_MAIN, run_name="__main__")
        return started

    def test_main_block_port_from_env(self, monkeypatch):
        monkeypatch.setenv("PORT", "9125")
        assert self._run_main(monkeypatch) == [
            (("main:app",), {"host": "0.0.0.0", "port": 9125, "reload": True})]

    def test_main_block_port_default(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        assert self._run_main(monkeypatch)[0][1]["port"] == 8003


# ── group170: guarded market-data door + readable failure logs ───────────────
class TestMdGuardWiring:
    def test_guard_unavailable_falls_back_to_plain_httpx(self, env, monkeypatch):
        monkeypatch.setattr(fm, "_md_guard_mod", None)
        monkeypatch.setattr(fm, "_md_guard_tried", True)
        out = env.analyze({"pe_ratio": 20})
        assert out["fallback_used"] is False and env.urls
        assert fm._exc_detail(httpx.ReadTimeout("")) == "ReadTimeout"
        assert fm._exc_detail(httpx.ReadTimeout("slow")) == "ReadTimeout: slow"

    def test_guard_import_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "md_guard", None)
        monkeypatch.setattr(fm, "_md_guard_mod", "stale")
        monkeypatch.setattr(fm, "_md_guard_tried", False)
        assert fm._guard() is None and fm._md_guard_tried is True

    def test_guard_is_used_when_available(self, env, monkeypatch):
        import md_guard
        monkeypatch.setattr(fm, "_md_guard_mod", md_guard)
        monkeypatch.setattr(fm, "_md_guard_tried", True)
        seen = []
        monkeypatch.setattr(md_guard, "md_get", lambda url, **kw: seen.append((url, kw)) or _Resp({"pe_ratio": 20}))
        env.analyze({"pe_ratio": 20})
        assert seen and seen[0][1] == {"timeout": 60} and fm._exc_detail(httpx.ReadTimeout("")) == "ReadTimeout"

    def test_cooldown_error_falls_back_with_a_readable_warning(self, env, caplog):
        import md_guard
        env.http_exc = md_guard.MarketDataUnavailable("market-data cooling down after repeated timeouts (9s left)")
        with caplog.at_level(logging.WARNING):
            out = env.analyze()
        assert out["fallback_used"] is True and out["fundamental_score"] == 50
        assert "unavailable for TESTCO" in caplog.text and "MarketDataUnavailable" in caplog.text

    def test_timeout_log_names_the_exception_type(self, env, caplog):
        env.http_exc = httpx.ReadTimeout("")
        with caplog.at_level(logging.WARNING):
            env.analyze()
        assert "timed out for TESTCO (ReadTimeout)" in caplog.text

    def test_unexpected_error_log_has_symbol_and_type(self, env, caplog):
        env.http_exc = RuntimeError("")
        with caplog.at_level(logging.ERROR):
            assert env.analyze()["fallback_used"] is True
        assert "TESTCO: RuntimeError" in caplog.text
