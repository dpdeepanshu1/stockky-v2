"""
tests/test_sentiment_main.py — coverage for sentiment/main.py

yfinance and pandas/numpy stubbed before import. FastAPI TestClient used
for route tests. All yfinance calls are monkeypatched.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_sentiment_main.py -v
"""
from __future__ import annotations
import asyncio, os, sys, types, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sentiment"))

# ── stub heavy deps before import ─────────────────────────────────────────────
import numpy as _np
import pandas as _pd
import requests as _requests

for _name in ("yfinance",):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        _stub.set_session = lambda s: None
        _stub.shared = types.SimpleNamespace(_session=None)
        class _FakeTicker:
            def __init__(self, sym): self.sym = sym
            def history(self, **kw):
                return _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})
        _stub.Ticker = _FakeTicker
        _stub.download = lambda **kw: _pd.DataFrame()
        sys.modules[_name] = _stub

import main as sm   # sentiment/main.py
from fastapi.testclient import TestClient
client = TestClient(sm.app, raise_server_exceptions=False)

import pytest


@pytest.fixture(autouse=True)
def _clean_cache():
    sm._cache["data"] = None
    sm._cache["timestamp"] = None
    yield
    sm._cache["data"] = None
    sm._cache["timestamp"] = None


# ── _safe_float ───────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_normal(self):   assert sm._safe_float(3.14) == 3.14
    def test_none(self):     assert sm._safe_float(None) is None
    def test_nan(self):      assert sm._safe_float(float("nan")) is None
    def test_inf(self):      assert sm._safe_float(float("inf")) is None
    def test_string(self):   assert sm._safe_float("25.5") == 25.5
    def test_bad_str(self):  assert sm._safe_float("N/A") is None
    def test_rounding(self): assert sm._safe_float(3.14159) == 3.14


# ── _safe_int ─────────────────────────────────────────────────────────────────

class TestSafeInt:
    def test_normal(self):   assert sm._safe_int(42) == 42
    def test_none(self):     assert sm._safe_int(None) is None
    def test_float(self):    assert sm._safe_int(3.9) == 3
    def test_bad_str(self):  assert sm._safe_int("abc") is None


# ── classify_sentiment ────────────────────────────────────────────────────────

class TestClassifySentiment:
    def test_75_strongly_bullish(self):  assert sm.classify_sentiment(75) == "STRONGLY BULLISH"
    def test_76_strongly_bullish(self):  assert sm.classify_sentiment(100) == "STRONGLY BULLISH"
    def test_55_bullish(self):           assert sm.classify_sentiment(55) == "BULLISH"
    def test_70_bullish(self):           assert sm.classify_sentiment(70) == "BULLISH"
    def test_50_neutral(self):           assert sm.classify_sentiment(50) == "NEUTRAL"
    def test_45_neutral(self):           assert sm.classify_sentiment(45) == "NEUTRAL"
    def test_44_bearish(self):           assert sm.classify_sentiment(44) == "BEARISH"
    def test_25_bearish(self):           assert sm.classify_sentiment(25) == "BEARISH"
    def test_24_strongly_bearish(self):  assert sm.classify_sentiment(24) == "STRONGLY BEARISH"
    def test_0_strongly_bearish(self):   assert sm.classify_sentiment(0) == "STRONGLY BEARISH"


# ── compute_market_score ──────────────────────────────────────────────────────

def _make_index(name, change_pct):
    from datetime import datetime
    return sm.IndexData(
        symbol="^TEST", name=name,
        current=100.0, previous_close=99.0,
        change=change_pct * 0.99, change_percent=change_pct,
        high=101.0, low=99.0, volume=1_000_000,
        timestamp=datetime.now(),
    )


class TestComputeMarketScore:
    def setup_method(self):
        import yfinance as yf

        class _EmptyTicker:
            def __init__(self, sym): pass
            def history(self, **kw):
                return _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})

        yf.Ticker = _EmptyTicker   # suppress momentum + volatility adjustments

    def test_empty_returns_50(self):
        assert sm.compute_market_score({}) == 50

    def test_no_change_percent_returns_50(self):
        from datetime import datetime
        idx = sm.IndexData(symbol="X", name="NIFTY 50",
                           current=None, previous_close=None,
                           change=None, change_percent=None,
                           high=None, low=None, volume=None,
                           timestamp=datetime.now())
        assert sm.compute_market_score({"NIFTY 50": idx}) == 50

    def test_positive_change_above_50(self):
        data = {"NIFTY 50": _make_index("NIFTY 50", 0.3),
                "SENSEX": _make_index("SENSEX", 0.3)}
        assert sm.compute_market_score(data) >= 50

    def test_negative_change_below_50(self):
        data = {"NIFTY 50": _make_index("NIFTY 50", -0.3),
                "SENSEX": _make_index("SENSEX", -0.3)}
        assert sm.compute_market_score(data) <= 50

    def test_large_positive_capped_at_100(self):
        data = {"NIFTY 50": _make_index("NIFTY 50", 5.0),
                "SENSEX": _make_index("SENSEX", 5.0)}
        score = sm.compute_market_score(data)
        assert 0 <= score <= 100

    def test_returns_int(self):
        data = {"NIFTY 50": _make_index("NIFTY 50", 0.1)}
        assert isinstance(sm.compute_market_score(data), int)

    def test_weighted_average_nifty_heavier(self):
        # NIFTY positive, SENSEX negative; NIFTY weight 0.6 should dominate
        data = {"NIFTY 50": _make_index("NIFTY 50", 0.3),
                "SENSEX": _make_index("SENSEX", -0.3)}
        score = sm.compute_market_score(data)
        # weighted avg = 0.6*0.3 + 0.4*(-0.3) = 0.18 - 0.12 = 0.06 → positive → > 50
        assert score > 50

    def test_momentum_adjustment_applied(self, monkeypatch):
        """When 6-day history is available the momentum path runs."""
        import yfinance as yf
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 106.0]
        class _Ticker:
            def __init__(self, sym): pass
            def history(self, **kw):
                period = kw.get("period", "")
                if "6d" in period or period == "6d":
                    return _pd.DataFrame({"Close": closes, "High": closes,
                                          "Low": closes, "Volume": [0]*6})
                return _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})
        yf.Ticker = _Ticker
        data = {"NIFTY 50": _make_index("NIFTY 50", 0.2)}
        score = sm.compute_market_score(data)
        assert 0 <= score <= 100

    def test_volatility_adjustment_applied(self, monkeypatch):
        """When 1mo history is available volatility path runs."""
        import yfinance as yf
        n = 20
        class _Ticker:
            def __init__(self, sym): pass
            def history(self, **kw):
                period = kw.get("period", "")
                if period == "1mo":
                    return _pd.DataFrame({
                        "Close": [100.0]*n,
                        "High": [101.0]*n,
                        "Low": [99.0]*n,
                        "Volume": [0]*n,
                    })
                return _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})
        yf.Ticker = _Ticker
        data = {"NIFTY 50": _make_index("NIFTY 50", 0.1)}
        score = sm.compute_market_score(data)
        assert 0 <= score <= 100


# ── fetch_individual_ticker ───────────────────────────────────────────────────

class TestFetchIndividualTicker:
    def test_returns_none_on_empty_history(self, monkeypatch):
        import yfinance as yf
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw): return _pd.DataFrame()
        yf.Ticker = _T
        result = sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=1)
        assert result is None

    def test_returns_index_data_on_two_rows(self, monkeypatch):
        import yfinance as yf
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                return _pd.DataFrame({
                    "Close": [100.0, 102.0], "High": [103.0, 104.0],
                    "Low": [99.0, 101.0], "Volume": [1000000, 1100000],
                })
        yf.Ticker = _T
        result = sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=1)
        assert result is not None
        assert result.current == 102.0
        assert result.previous_close == 100.0
        assert result.change_percent is not None

    def test_returns_none_only_one_row(self, monkeypatch):
        import yfinance as yf
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                return _pd.DataFrame({"Close": [100.0], "High": [101.0], "Low": [99.0], "Volume": [0]})
        yf.Ticker = _T
        result = sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=1)
        assert result is None

    def test_retries_on_exception(self, monkeypatch):
        import yfinance as yf
        call_count = [0]
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                call_count[0] += 1
                if call_count[0] < 2:
                    raise RuntimeError("transient")
                return _pd.DataFrame({
                    "Close": [100.0, 102.0], "High": [103.0, 104.0],
                    "Low": [99.0, 101.0], "Volume": [1000000, 1100000],
                })
        yf.Ticker = _T
        monkeypatch.setattr(sm.time, "sleep", lambda s: None)
        result = sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=2)
        assert result is not None

    def test_exhausts_retries_returns_none(self, monkeypatch):
        import yfinance as yf
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw): raise RuntimeError("always fails")
        yf.Ticker = _T
        monkeypatch.setattr(sm.time, "sleep", lambda s: None)
        result = sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=2)
        assert result is None


# ── fetch_indices_batch ───────────────────────────────────────────────────────

class TestFetchIndicesBatch:
    def test_empty_symbols_returns_empty(self):
        assert sm.fetch_indices_batch({}) == {}

    def test_returns_index_data_from_batch(self, monkeypatch):
        import yfinance as yf
        closes = [100.0, 102.0, 103.0]
        highs = [104.0, 105.0, 106.0]
        lows = [99.0, 100.0, 101.0]
        vols = [1_000_000, 1_100_000, 1_200_000]
        import pandas as pd
        tuples = [("^NSEI", "Close"), ("^NSEI", "High"), ("^NSEI", "Low"), ("^NSEI", "Volume")]
        idx = pd.MultiIndex.from_tuples(tuples)
        df = pd.DataFrame(
            [closes, highs, lows, vols], index=idx
        ).T
        df.columns = pd.MultiIndex.from_tuples(tuples)
        yf.download = lambda **kw: df
        result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI"})
        assert "NIFTY 50" in result
        assert result["NIFTY 50"].current == 103.0

    def test_falls_back_to_individual_on_empty_batch(self, monkeypatch):
        import yfinance as yf
        yf.download = lambda **kw: _pd.DataFrame()
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                return _pd.DataFrame({
                    "Close": [100.0, 102.0], "High": [103.0, 104.0],
                    "Low": [99.0, 101.0], "Volume": [1000000, 1100000],
                })
        yf.Ticker = _T
        result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI"})
        assert "NIFTY 50" in result

    def test_falls_back_on_download_exception(self, monkeypatch):
        import yfinance as yf
        call_count = [0]
        def _fail(**kw):
            call_count[0] += 1
            raise RuntimeError("download error")
        yf.download = _fail
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                return _pd.DataFrame({
                    "Close": [100.0, 102.0], "High": [103.0, 104.0],
                    "Low": [99.0, 101.0], "Volume": [1000000, 1100000],
                })
        yf.Ticker = _T
        monkeypatch.setattr(sm.time, "sleep", lambda s: None)
        result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI"})
        assert "NIFTY 50" in result


# ── route tests ───────────────────────────────────────────────────────────────

class TestRoutes:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_root(self):
        r = client.get("/")
        body = r.json()
        assert body["status"] == "running"
        assert "version" in body

    def test_sentiment_returns_neutral_when_no_data(self, monkeypatch):
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {})
        r = client.get("/sentiment")
        body = r.json()
        assert body["market_score"] == 50
        assert body["classification"] == "NEUTRAL"
        assert body["stale"] is True

    def test_sentiment_returns_real_score(self, monkeypatch):
        from datetime import datetime
        nifty = sm.IndexData(
            symbol="^NSEI", name="NIFTY 50", current=24500.0,
            previous_close=24000.0, change=500.0, change_percent=2.08,
            high=24600.0, low=24100.0, volume=2_000_000,
            timestamp=datetime.now()
        )
        sensex = sm.IndexData(
            symbol="^BSESN", name="SENSEX", current=80000.0,
            previous_close=78500.0, change=1500.0, change_percent=1.91,
            high=80500.0, low=79000.0, volume=1_500_000,
            timestamp=datetime.now()
        )
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {"NIFTY 50": nifty, "SENSEX": sensex})
        import yfinance as yf
        class _Empty:
            def __init__(self, sym): pass
            def history(self, **kw): return _pd.DataFrame()
        yf.Ticker = _Empty
        r = client.get("/sentiment")
        body = r.json()
        assert body["market_score"] >= 50   # positive change → bullish
        assert body["cached"] is False

    def test_sentiment_served_from_cache(self, monkeypatch):
        from datetime import datetime
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {})
        client.get("/sentiment")   # warm cache
        call_count = [0]
        def _count(syms):
            call_count[0] += 1
            return {}
        monkeypatch.setattr(sm, "fetch_indices_batch", _count)
        r = client.get("/sentiment")
        assert r.json()["cached"] is True
        assert call_count[0] == 0

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        from datetime import datetime
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {})
        client.get("/sentiment")   # warm cache
        call_count = [0]
        def _count(syms):
            call_count[0] += 1
            return {}
        monkeypatch.setattr(sm, "fetch_indices_batch", _count)
        r = client.get("/sentiment?force_refresh=true")
        assert call_count[0] == 1

    def test_sentiment_returns_stale_on_data_failure(self, monkeypatch):
        """Second call: data unavailable → returns stale cached data."""
        from datetime import datetime
        nifty = sm.IndexData(
            symbol="^NSEI", name="NIFTY 50", current=24000.0,
            previous_close=23800.0, change=200.0, change_percent=0.84,
            high=24100.0, low=23700.0, volume=1_000_000, timestamp=datetime.now()
        )
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {"NIFTY 50": nifty})
        import yfinance as yf
        class _Empty:
            def __init__(self, sym): pass
            def history(self, **kw): return _pd.DataFrame()
        yf.Ticker = _Empty
        # Warm cache
        client.get("/sentiment?force_refresh=true")
        # Expire cache artificially
        from datetime import timedelta
        sm._cache["timestamp"] = datetime.now() - timedelta(seconds=9999)
        # Now return empty data
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {})
        r = client.get("/sentiment?force_refresh=true")
        body = r.json()
        assert body["stale"] is True

    def test_trend_and_breadth_computed(self, monkeypatch):
        from datetime import datetime
        nifty = sm.IndexData(
            symbol="^NSEI", name="NIFTY 50", current=100.0,
            previous_close=99.0, change=1.0, change_percent=1.01,
            high=101.0, low=99.0, volume=500_000, timestamp=datetime.now()
        )
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {"NIFTY 50": nifty})
        import yfinance as yf
        class _Empty:
            def __init__(self, sym): pass
            def history(self, **kw): return _pd.DataFrame()
        yf.Ticker = _Empty
        r = client.get("/sentiment?force_refresh=true")
        body = r.json()
        assert body["trend"] in ("Bullish", "Bearish", "Neutral")
        assert body["breadth"] in ("Positive", "Negative", "Mixed")
        assert body["momentum"] in ("Strong", "Weak", "Moderate")
        assert body["volatility"] in ("Normal", "High")


# ══ session 125: fallback / exception / double-check / __main__ branches ═════════════════

import logging
import runpy


def _two_row_hist():
    return _pd.DataFrame({
        "Close": [100.0, 102.0], "High": [103.0, 104.0],
        "Low": [99.0, 101.0], "Volume": [1_000_000, 1_100_000],
    })


def _ticker_class(hist=None, exc=None):
    """yf.Ticker stand-in: history() returns `hist` (or raises `exc`)."""
    class _T:
        def __init__(self, sym): pass
        def history(self, **kw):
            if exc is not None:
                raise exc
            return hist if hist is not None else _pd.DataFrame()
    return _T


def _batch_df(sym, n=3, start=200.0, cols=("Close", "High", "Low", "Volume")):
    base = {
        "Close": [start + i for i in range(n)],
        "High": [start + 4 + i for i in range(n)],
        "Low": [start - 1 + i for i in range(n)],
        "Volume": [1_000_000 + i for i in range(n)],
    }
    return _pd.DataFrame({(sym, c): base[c] for c in cols})


@pytest.fixture
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sm.time, "sleep", lambda s: None)


class TestFetchIndividualTickerZeroRetries:
    def test_zero_retries_never_calls_yahoo_and_returns_none(self, monkeypatch):
        import yfinance as yf
        calls = []
        class _T:
            def __init__(self, sym): calls.append(sym)
        monkeypatch.setattr(yf, "Ticker", _T)
        assert sm.fetch_individual_ticker("^NSEI", "NIFTY 50", max_retries=0) is None
        assert calls == []


class TestBatchFallbackWhenIndividualAlsoFails:
    def test_download_exception_and_individual_fail_logs_and_returns_empty(self, monkeypatch, caplog, _no_sleep):
        import yfinance as yf
        def _boom(**kw): raise RuntimeError("download error")
        monkeypatch.setattr(yf, "download", _boom)
        monkeypatch.setattr(yf, "Ticker", _ticker_class(hist=_pd.DataFrame()))
        with caplog.at_level(logging.WARNING, logger="market-sentiment-service"):
            result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI"})
        assert result == {}
        assert any("Individual fetch also failed for NIFTY 50" in r.getMessage() for r in caplog.records)

    def test_empty_batch_and_individual_fail_logs_and_returns_empty(self, monkeypatch, caplog, _no_sleep):
        import yfinance as yf
        monkeypatch.setattr(yf, "download", lambda **kw: _pd.DataFrame())
        monkeypatch.setattr(yf, "Ticker", _ticker_class(hist=_pd.DataFrame()))
        with caplog.at_level(logging.WARNING, logger="market-sentiment-service"):
            result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI"})
        assert result == {}
        assert any("Individual fetch failed for NIFTY 50" in r.getMessage() for r in caplog.records)

    def test_empty_batch_partial_individual_success_keeps_the_good_one(self, monkeypatch, _no_sleep):
        import yfinance as yf
        monkeypatch.setattr(yf, "download", lambda **kw: None)
        class _T:
            def __init__(self, sym): self.sym = sym
            def history(self, **kw):
                return _two_row_hist() if self.sym == "^NSEI" else _pd.DataFrame()
        monkeypatch.setattr(yf, "Ticker", _T)
        result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI", "SENSEX": "^BSESN"})
        assert list(result) == ["NIFTY 50"]


class TestBatchPerSymbolFallbacks:
    SYMS = {"NIFTY 50": "^NSEI"}

    def _patch(self, monkeypatch, df, hist):
        import yfinance as yf
        monkeypatch.setattr(yf, "download", lambda **kw: df)
        monkeypatch.setattr(yf, "Ticker", _ticker_class(hist=hist))

    # symbol absent from the batch frame (206-210)
    def test_symbol_missing_from_batch_uses_individual_fetch(self, monkeypatch, caplog):
        self._patch(monkeypatch, _batch_df("^OTHER"), _two_row_hist())
        with caplog.at_level(logging.WARNING, logger="market-sentiment-service"):
            result = sm.fetch_indices_batch(self.SYMS)
        assert result["NIFTY 50"].current == 102.0
        assert any("No data for NIFTY 50 (^NSEI)" in r.getMessage() for r in caplog.records)

    def test_symbol_missing_from_batch_and_individual_fails_is_dropped(self, monkeypatch):
        self._patch(monkeypatch, _batch_df("^OTHER"), _pd.DataFrame())
        assert sm.fetch_indices_batch(self.SYMS) == {}

    # only one row of batch data (233-236)
    def test_single_row_in_batch_uses_individual_fetch(self, monkeypatch, caplog):
        self._patch(monkeypatch, _batch_df("^NSEI", n=1), _two_row_hist())
        with caplog.at_level(logging.WARNING, logger="market-sentiment-service"):
            result = sm.fetch_indices_batch(self.SYMS)
        assert result["NIFTY 50"].current == 102.0
        assert any("Insufficient data for NIFTY 50 (^NSEI)" in r.getMessage() for r in caplog.records)

    def test_single_row_in_batch_and_individual_fails_is_dropped(self, monkeypatch):
        self._patch(monkeypatch, _batch_df("^NSEI", n=1), _pd.DataFrame())
        assert sm.fetch_indices_batch(self.SYMS) == {}

    # processing blows up (237-241): 'Close' column absent -> KeyError
    def test_processing_error_falls_back_to_individual(self, monkeypatch, caplog):
        df = _batch_df("^NSEI", cols=("High", "Low", "Volume"))
        self._patch(monkeypatch, df, _two_row_hist())
        with caplog.at_level(logging.ERROR, logger="market-sentiment-service"):
            result = sm.fetch_indices_batch(self.SYMS)
        assert result["NIFTY 50"].current == 102.0
        assert any("Error processing NIFTY 50 (^NSEI)" in r.getMessage() for r in caplog.records)

    def test_processing_error_and_individual_fails_is_dropped(self, monkeypatch):
        df = _batch_df("^NSEI", cols=("High", "Low", "Volume"))
        self._patch(monkeypatch, df, _pd.DataFrame())
        assert sm.fetch_indices_batch(self.SYMS) == {}

    def test_one_bad_symbol_does_not_affect_a_good_one(self, monkeypatch):
        # batch has only ^NSEI (start=200) -> SENSEX must come from the individual path (102.0)
        self._patch(monkeypatch, _batch_df("^NSEI"), _two_row_hist())
        result = sm.fetch_indices_batch({"NIFTY 50": "^NSEI", "SENSEX": "^BSESN"})
        assert result["NIFTY 50"].current == 202.0
        assert result["SENSEX"].current == 102.0


class TestComputeMarketScoreAdjustmentFailures:
    DATA = staticmethod(lambda: {"NIFTY 50": _make_index("NIFTY 50", 0.2)})

    def _baseline(self, monkeypatch):
        import yfinance as yf
        monkeypatch.setattr(yf, "Ticker", _ticker_class(hist=_pd.DataFrame(
            {"Close": [], "High": [], "Low": [], "Volume": []})))
        return sm.compute_market_score(self.DATA())

    @pytest.mark.parametrize("period,message", [
        ("6d", "Could not compute 5-day momentum"),
        ("1mo", "Could not compute volatility adjustment"),
    ])
    def test_yahoo_failure_skips_that_adjustment_only(self, monkeypatch, caplog, period, message):
        import yfinance as yf
        baseline = self._baseline(monkeypatch)
        empty = _pd.DataFrame({"Close": [], "High": [], "Low": [], "Volume": []})
        class _T:
            def __init__(self, sym): pass
            def history(self, **kw):
                if kw.get("period") == period:
                    raise RuntimeError("yahoo down")
                return empty
        monkeypatch.setattr(yf, "Ticker", _T)
        with caplog.at_level(logging.DEBUG, logger="market-sentiment-service"):
            score = sm.compute_market_score(self.DATA())
        assert score == baseline
        assert any(message in r.getMessage() and "yahoo down" in r.getMessage() for r in caplog.records)

    def test_ticker_constructor_failure_is_swallowed_for_both_adjustments(self, monkeypatch, caplog):
        import yfinance as yf
        baseline = self._baseline(monkeypatch)
        def _bad(sym): raise RuntimeError("no ticker")
        monkeypatch.setattr(yf, "Ticker", _bad)
        with caplog.at_level(logging.DEBUG, logger="market-sentiment-service"):
            assert sm.compute_market_score(self.DATA()) == baseline
        msgs = [r.getMessage() for r in caplog.records]
        assert any("5-day momentum" in m for m in msgs)
        assert any("volatility adjustment" in m for m in msgs)


class _PopulatingLock:
    """Async-lock stand-in: while the request 'waits' for it, another request fills the cache."""
    def __init__(self, timestamp_set=True):
        self.timestamp_set = timestamp_set

    async def __aenter__(self):
        sm._cache["data"] = {
            "timestamp": sm.datetime.now(), "indices": {}, "market_score": 61,
            "classification": "BULLISH", "trend": "Bullish", "momentum": "Moderate",
            "breadth": "Mixed", "volatility": "Normal", "cached": False, "stale": False,
        }
        if self.timestamp_set:
            sm._cache["timestamp"] = sm.datetime.now()

    async def __aexit__(self, *exc):
        return False


class TestDoubleCheckedCacheInsideLock:
    def test_cache_filled_while_waiting_for_lock_is_served_without_refetch(self, monkeypatch):
        def _must_not_fetch(syms): raise AssertionError("should have been served from cache")
        monkeypatch.setattr(sm, "fetch_indices_batch", _must_not_fetch)
        monkeypatch.setitem(sm._cache, "lock", _PopulatingLock())
        body = client.get("/sentiment").json()
        assert body["market_score"] == 61
        assert body["cached"] is True
        assert body["stale"] is False

    def test_cache_without_timestamp_inside_lock_counts_as_expired(self, monkeypatch):
        # data present but timestamp None -> age treated as 9999 -> refetch; fetch fails -> stale copy
        monkeypatch.setattr(sm, "fetch_indices_batch", lambda syms: {})
        monkeypatch.setitem(sm._cache, "lock", _PopulatingLock(timestamp_set=False))
        body = client.get("/sentiment").json()
        assert body["market_score"] == 61
        assert body["cached"] is True and body["stale"] is True


class TestMainEntrypoint:
    def _run(self, monkeypatch, port_env):
        calls = []
        fake = types.ModuleType("uvicorn")
        fake.run = lambda app, **kw: calls.append((app, kw))
        monkeypatch.setitem(sys.modules, "uvicorn", fake)
        if port_env is None:
            monkeypatch.delenv("PORT", raising=False)
        else:
            monkeypatch.setenv("PORT", port_env)
        runpy.run_path(sm.__file__, run_name="__main__")
        return calls

    def test_default_port_8009(self, monkeypatch):
        from fastapi import FastAPI
        calls = self._run(monkeypatch, None)
        assert len(calls) == 1
        assert isinstance(calls[0][0], FastAPI)
        assert calls[0][1] == {"host": "0.0.0.0", "port": 8009, "reload": True}

    def test_port_from_environment(self, monkeypatch):
        calls = self._run(monkeypatch, "9123")
        assert calls[0][1]["port"] == 9123

