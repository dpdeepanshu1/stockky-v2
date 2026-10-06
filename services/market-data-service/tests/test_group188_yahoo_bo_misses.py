"""
group188 (item 10): do not ask Yahoo for NAME.BO when NAME.NS has nothing.

The 2026-10-06 boot log had ~15 "$CHEMICAL.BO / AJOONI.BO / SBILIQETF.BO ...: possibly delisted; no price data
found" errors. Every NSE symbol was tried as NAME.NS and then NAME.BO. Now a symbol in the AngelOne NSE scrip
master skips .BO, and any other .BO miss is remembered for YAHOO_BO_MISS_TTL_S.

Run from services/market-data-service:
    python3 -m pytest tests/test_group188_yahoo_bo_misses.py -q
"""
from __future__ import annotations
import os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as m
import angelone_scrip_master as sm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    m._YF_BO_MISS.clear()
    m._UPSTREAM_COOLDOWN.clear()
    for k in ("YAHOO_SKIP_BO_FOR_NSE", "YAHOO_BO_MISS_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sm, "_token_map", {})
    monkeypatch.setattr(sm, "_be_map", {})
    yield
    m._YF_BO_MISS.clear()


def _master(monkeypatch, eq=(), be=()):
    monkeypatch.setattr(sm, "_token_map", {s: "1" for s in eq})
    monkeypatch.setattr(sm, "_be_map", {s: "2" for s in be})


class _EmptyHist:
    empty = True
    columns = ()


def _fake_yf(monkeypatch, empty_for=(), calls=None):
    """yf.Ticker(t).history(...) -> empty for tickers in empty_for, a one-row frame otherwise."""
    import pandas as pd

    class _T:
        def __init__(self, t):
            self.t = t

        def history(self, *a, **kw):
            if calls is not None:
                calls.append(self.t)
            if self.t in empty_for:
                return pd.DataFrame()
            return pd.DataFrame({"Open": [10.0], "High": [11.0], "Low": [9.0], "Close": [10.5], "Volume": [100]})

    monkeypatch.setattr(m.yf, "Ticker", _T, raising=False)


class TestIsListed:
    def test_none_when_master_not_loaded(self):
        assert sm.is_listed("RELIANCE") is None

    def test_true_for_eq_and_be(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"], be=["BMISL"])
        assert sm.is_listed("RELIANCE") is True
        assert sm.is_listed("bmisl.ns") is True

    def test_false_when_loaded_and_absent(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"])
        assert sm.is_listed("AJOONI") is False

    def test_suffixes_ignored(self, monkeypatch):
        _master(monkeypatch, eq=["TCS"])
        assert sm.is_listed("TCS.BO") is True


class TestCandidates:
    def test_master_not_loaded_keeps_both(self):
        assert m._yahoo_tickers_for("RELIANCE") == ["RELIANCE.NS", "RELIANCE.BO"]

    def test_nse_listed_skips_bo(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"])
        assert m._yahoo_tickers_for("RELIANCE") == ["RELIANCE.NS"]

    def test_symbol_not_in_master_keeps_bo(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"])
        assert m._yahoo_tickers_for("AJOONI") == ["AJOONI.NS", "AJOONI.BO"]

    def test_skip_switch_off_restores_bo(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"])
        monkeypatch.setenv("YAHOO_SKIP_BO_FOR_NSE", "0")
        assert m._yahoo_tickers_for("RELIANCE") == ["RELIANCE.NS", "RELIANCE.BO"]

    def test_rename_is_checked_on_the_mapped_name(self, monkeypatch):
        _master(monkeypatch, eq=["ETERNAL"])
        assert m._yahoo_tickers_for("ZOMATO") == ["ETERNAL.NS"]

    def test_index_unchanged(self, monkeypatch):
        _master(monkeypatch, eq=["RELIANCE"])
        assert m._yahoo_tickers_for("^NSEI") == ["^NSEI"]
        assert m._yahoo_tickers_for("NIFTY50") == ["^NSEI"]


class TestMissMemory:
    def test_bo_miss_is_remembered(self):
        m._yahoo_bo_note_miss("AJOONI.BO")
        assert m._yahoo_tickers_for("AJOONI") == ["AJOONI.NS"]

    def test_ns_miss_is_not_remembered(self):
        m._yahoo_bo_note_miss("AJOONI.NS")
        assert m._yahoo_tickers_for("AJOONI") == ["AJOONI.NS", "AJOONI.BO"]

    def test_other_symbols_unaffected(self):
        m._yahoo_bo_note_miss("AJOONI.BO")
        assert m._yahoo_tickers_for("TCS") == ["TCS.NS", "TCS.BO"]

    def test_expires(self, monkeypatch):
        m._yahoo_bo_note_miss("AJOONI.BO")
        real = m.time.time
        monkeypatch.setattr(m.time, "time", lambda: real() + 21600 + 5)
        assert m._yahoo_tickers_for("AJOONI") == ["AJOONI.NS", "AJOONI.BO"]
        assert "AJOONI" not in m._YF_BO_MISS

    def test_ttl_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("YAHOO_BO_MISS_TTL_S", "0")
        m._yahoo_bo_note_miss("AJOONI.BO")
        assert m._YF_BO_MISS == {}
        assert m._yahoo_tickers_for("AJOONI") == ["AJOONI.NS", "AJOONI.BO"]

    def test_bad_ttl_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("YAHOO_BO_MISS_TTL_S", "abc")
        m._yahoo_bo_note_miss("AJOONI.BO")
        assert "AJOONI" in m._YF_BO_MISS

    def test_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(m, "_YF_BO_MISS_MAX", 3)
        for n in ("A", "B", "C", "D"):
            m._yahoo_bo_note_miss(f"{n}.BO")
        assert len(m._YF_BO_MISS) == 3 and "D" not in m._YF_BO_MISS

    def test_full_table_drops_expired_first(self, monkeypatch):
        monkeypatch.setattr(m, "_YF_BO_MISS_MAX", 2)
        m._YF_BO_MISS.update({"OLD1": 1.0, "OLD2": 1.0})
        m._yahoo_bo_note_miss("NEW.BO")
        assert "NEW" in m._YF_BO_MISS and "OLD1" not in m._YF_BO_MISS


class TestOhlcvQuote:
    def test_bo_miss_recorded_then_not_retried(self, monkeypatch):
        calls = []
        _fake_yf(monkeypatch, empty_for=("AJOONI.NS", "AJOONI.BO"), calls=calls)
        assert m._yahoo_ohlcv_quote("AJOONI") is None
        assert calls == ["AJOONI.NS", "AJOONI.BO"]
        calls.clear()
        assert m._yahoo_ohlcv_quote("AJOONI") is None
        assert calls == ["AJOONI.NS"]

    def test_bse_only_symbol_still_found_via_bo(self, monkeypatch):
        calls = []
        _fake_yf(monkeypatch, empty_for=("BSEONLY.NS",), calls=calls)
        q = m._yahoo_ohlcv_quote("BSEONLY")
        assert q and q["yahoo_ticker"] == "BSEONLY.BO"
        assert m._YF_BO_MISS == {}

    def test_ns_hit_never_touches_bo(self, monkeypatch):
        calls = []
        _fake_yf(monkeypatch, calls=calls)
        assert m._yahoo_ohlcv_quote("TCS")["yahoo_ticker"] == "TCS.NS"
        assert calls == ["TCS.NS"]

    def test_nse_listed_symbol_asks_yahoo_once(self, monkeypatch):
        _master(monkeypatch, eq=["AJOONI"])
        calls = []
        _fake_yf(monkeypatch, empty_for=("AJOONI.NS", "AJOONI.BO"), calls=calls)
        assert m._yahoo_ohlcv_quote("AJOONI") is None
        assert calls == ["AJOONI.NS"]

    def test_history_price_path_records_bo_miss(self, monkeypatch):
        calls = []
        _fake_yf(monkeypatch, empty_for=("AJOONI.NS", "AJOONI.BO"), calls=calls)
        assert m._waterfall_yahoo_history_price("AJOONI") is None
        assert "AJOONI" in m._YF_BO_MISS
        calls.clear()
        m._waterfall_yahoo_history_price("AJOONI")
        assert calls == ["AJOONI.NS"]
