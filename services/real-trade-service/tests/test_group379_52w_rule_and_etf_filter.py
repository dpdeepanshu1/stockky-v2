"""group379 (Group E1 + E2 of the structure group378 report)

E1  the 52-week "top 12%" rule
    * applies only while the market is actually weak (market score below CANDIDATE_52W_WEAK_REGIME_SCORE); a missing
      or stale score keeps the rule on, CANDIDATE_52W_REGIME_GATE=0 restores "every regime",
    * does not reject a stock AT a fresh, volume-confirmed 52-week high (and the near-resistance check does not
      reject that same breakout), CANDIDATE_52W_FRESH_HIGH_EXEMPT=0 restores the old behaviour,
    * CANDIDATE_52W_RULE_ENABLED=0 switches the rule off,
    * the reject reason quotes the real market score.
E2  ETFs / index funds (ITBEES, ITIETF ...) are dropped by name before any quote / history / market-cap call, in both
    candidate tracks; one shared name test (watchlist_engine/symbol_filter.is_etf_symbol).

Run from services/real-trade-service:  python3 -m pytest tests/test_group379_52w_rule_and_etf_filter.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import intraday_eligibility
from candidate_engine import candidates as cd
from watchlist_engine import symbol_filter as sf
from entry_engine import entry

from test_candidates_analysis import (  # noqa: E402  (same folder; pytest puts tests/ on sys.path)
    _RoutedAsyncClient, _history_router, _happy_by_tf, _c,
)

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _reset_52w_state(monkeypatch):
    """Every test starts from the shipped defaults with no market score."""
    monkeypatch.setattr(cd, "_cycle_market_score", None)
    monkeypatch.setattr(cd, "RANGE_52W_ENABLED", True)
    monkeypatch.setattr(cd, "RANGE_52W_REGIME_GATE", True)
    monkeypatch.setattr(cd, "RANGE_52W_WEAK_REGIME_SCORE", 50.0)
    monkeypatch.setattr(cd, "FRESH_HIGH_EXEMPT", True)
    monkeypatch.setattr(cd, "FRESH_HIGH_PCT", 1.0)
    monkeypatch.setattr(cd, "FRESH_HIGH_DAYS", 10.0)
    monkeypatch.setattr(cd, "FRESH_HIGH_VOL_RATIO", 1.2)


# ── data builders ───────────────────────────────────────────────────────────────────────────────────────────

def _overextended_by_tf():
    """Quote price is 100 (set by _history_router); 52w low 50, high 101 -> position 98% of the range, in the top 12%.
    No dates on these bars, so it can never count as a fresh high."""
    by_tf = _happy_by_tf()
    by_tf["1y"] = [_c(96, open_=96, high=101, low=50), _c(100, open_=96, high=101, low=60)]
    return by_tf


def _dated(date, close, high, low, volume=1000.0):
    d = _c(close, open_=close, high=high, low=low, volume=volume)
    d["date"] = date
    return d


def _breakout_by_tf(high_date="2026-10-10", volume_spike=2000.0, dated=True):
    """Price 100 sits at the 52w high (100.2, within 1%); last bar 2026-10-14. The 1m (daily) series is 20 flat bars
    with highs just above 100 - so the near-resistance check WOULD reject it - and the last 5 bars carry `volume_spike`
    against 1000 before (spike 2000 -> 5-day avg / 20-day avg = 1.6)."""
    by_tf = _happy_by_tf()
    one_y = [
        _dated("2025-10-20", 60, 62, 50),
        _dated(high_date, 100, 100.2, 95),
        _dated("2026-10-14", 100, 100.1, 98),
    ]
    if not dated:
        for c in one_y:
            c.pop("date")
    by_tf["1y"] = one_y
    flat = []
    for i in range(20):
        c = _c(99.5, open_=99.5, high=100.5, low=99.0, volume=(volume_spike if i >= 15 else 1000.0))
        flat.append(c)
    by_tf["1m"] = flat
    return by_tf


def _analyse(by_tf):
    return run(cd._multi_tf_analysis(_RoutedAsyncClient(get_router=_history_router(by_tf)), "TCS"))


# ── E1: regime gate ─────────────────────────────────────────────────────────────────────────────────────────

class TestRegimeGate:
    def test_unknown_score_keeps_the_rule_and_says_so(self):
        r = _analyse(_overextended_by_tf())
        assert r["reject_kind"] == "range_52w"
        assert "market score unavailable" in r["reject_reason"]

    def test_weak_market_applies_and_quotes_the_real_score(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 32.0)
        r = _analyse(_overextended_by_tf())
        assert r["reject_kind"] == "range_52w"
        assert "market score 32" in r["reject_reason"] and "weak market" in r["reject_reason"]
        assert "choppy" not in r["reject_reason"]

    def test_strong_market_skips_the_rule(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 70.0)
        r = _analyse(_overextended_by_tf())
        assert r["reject_reason"] is None

    def test_score_equal_to_threshold_is_not_weak(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 50.0)
        assert cd._range_52w_applies() == (False, 50.0)
        monkeypatch.setattr(cd, "_cycle_market_score", 49.9)
        assert cd._range_52w_applies() == (True, 49.9)

    def test_gate_off_applies_in_every_regime(self, monkeypatch):
        monkeypatch.setattr(cd, "RANGE_52W_REGIME_GATE", False)
        monkeypatch.setattr(cd, "_cycle_market_score", 90.0)
        r = _analyse(_overextended_by_tf())
        assert r["reject_kind"] == "range_52w"
        assert "market score 90" in r["reject_reason"] and "weak market" not in r["reject_reason"]

    def test_rule_switched_off(self, monkeypatch):
        monkeypatch.setattr(cd, "RANGE_52W_ENABLED", False)
        r = _analyse(_overextended_by_tf())
        assert r["reject_reason"] is None

    def test_not_overextended_is_unaffected_by_regime(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 10.0)
        assert _analyse(_happy_by_tf())["reject_reason"] is None


# ── E1: the per-cycle market score ──────────────────────────────────────────────────────────────────────────

class TestCycleMarketScore:
    def test_newest_recent_row_is_used(self, db):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(models.MarketRegimeHistory(score=61.0, recorded_at=now - timedelta(minutes=20)))
        db.add(models.MarketRegimeHistory(score=34.0, recorded_at=now - timedelta(minutes=2)))
        db.commit()
        cd._refresh_cycle_market_score(db)
        assert cd._cycle_market_score == 34.0

    def test_stale_row_counts_as_unknown(self, db):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
        db.add(models.MarketRegimeHistory(score=80.0, recorded_at=old))
        db.commit()
        cd._cycle_market_score = 99.0          # a previous cycle's value must not survive
        cd._refresh_cycle_market_score(db)
        assert cd._cycle_market_score is None

    def test_empty_table_is_unknown(self, db):
        cd._cycle_market_score = 99.0
        cd._refresh_cycle_market_score(db)
        assert cd._cycle_market_score is None

    def test_read_failure_is_unknown_not_an_error(self):
        class Broken:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        cd._cycle_market_score = 99.0
        cd._refresh_cycle_market_score(Broken())
        assert cd._cycle_market_score is None


# ── E1: fresh-high exemption ────────────────────────────────────────────────────────────────────────────────

class TestFreshHighExemption:
    def test_fresh_confirmed_high_passes_both_52w_and_resistance_checks(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 20.0)       # weak market: the old rule would reject
        r = _analyse(_breakout_by_tf())
        assert r["reject_reason"] is None
        assert "fresh-high breakout" in r["market_note"] and "4d old" in r["market_note"]

    def test_without_the_exemption_the_same_stock_is_rejected(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 20.0)
        monkeypatch.setattr(cd, "FRESH_HIGH_EXEMPT", False)
        assert _analyse(_breakout_by_tf())["reject_kind"] == "range_52w"

    def test_exemption_also_lifts_the_near_resistance_reject(self, monkeypatch):
        monkeypatch.setattr(cd, "RANGE_52W_ENABLED", False)         # only check 7 is left to reject it
        monkeypatch.setattr(cd, "FRESH_HIGH_EXEMPT", False)
        assert _analyse(_breakout_by_tf())["reject_kind"] == "resistance"
        monkeypatch.setattr(cd, "FRESH_HIGH_EXEMPT", True)
        assert _analyse(_breakout_by_tf())["reject_reason"] is None

    def test_stale_high_is_not_exempt(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 20.0)
        r = _analyse(_breakout_by_tf(high_date="2026-08-01"))      # 74 days before the last bar
        assert r["reject_kind"] == "range_52w"

    def test_no_volume_confirmation_is_not_exempt(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 20.0)
        r = _analyse(_breakout_by_tf(volume_spike=1000.0))         # ratio 1.0 < 1.2
        assert r["reject_kind"] == "range_52w"

    def test_undated_bars_are_not_exempt(self, monkeypatch):
        monkeypatch.setattr(cd, "_cycle_market_score", 20.0)
        assert _analyse(_breakout_by_tf(dated=False))["reject_kind"] == "range_52w"

    def test_helper_edges(self, monkeypatch):
        bars = _breakout_by_tf()["1y"]
        vols = _breakout_by_tf()["1m"]
        assert cd._fresh_high_breakout(bars, vols, 100.0) is not None
        assert cd._fresh_high_breakout(bars, vols, 0.0) is None
        assert cd._fresh_high_breakout(bars, vols, 90.0) is None              # 10% under the high
        assert cd._fresh_high_breakout(bars, [], 100.0) is None               # no volume data
        assert cd._fresh_high_breakout(None, vols, 100.0) is None
        assert cd._fresh_high_breakout(bars[:1], vols, 100.0) is None
        assert cd._fresh_high_breakout([{"high": 0, "date": "x"}, {"high": 0, "date": "y"}], vols, 100.0) is None
        assert cd._fresh_high_breakout(bars, "junk", 100.0) is None
        monkeypatch.setattr(cd, "FRESH_HIGH_EXEMPT", False)
        assert cd._fresh_high_breakout(bars, vols, 100.0) is None

    def test_volume_ratio_helper(self):
        assert cd._volume_ratio_5_20(None) is None
        assert cd._volume_ratio_5_20([_c(1)] * 5) is None
        flat = [_c(1, volume=100.0) for _ in range(20)]
        assert cd._volume_ratio_5_20(flat) == pytest.approx(1.0)
        flat += [{"volume": "bad"}]
        assert cd._volume_ratio_5_20(flat) == pytest.approx(1.0)

    def test_env_helpers(self, monkeypatch):
        monkeypatch.setenv("G379_X", "off")
        assert cd._env_flag_on("G379_X") is False
        monkeypatch.setenv("G379_X", "")
        assert cd._env_flag_on("G379_X") is True
        assert cd._env_flag_on("G379_X", "0") is False
        monkeypatch.setenv("G379_F", "abc")
        assert cd._env_float_or("G379_F", 7.5) == 7.5
        monkeypatch.setenv("G379_F", " 3.25 ")
        assert cd._env_float_or("G379_F", 7.5) == 3.25


# ── E2: the shared ETF name test ────────────────────────────────────────────────────────────────────────────

class TestIsEtfSymbol:
    @pytest.mark.parametrize("sym", [
        "ITBEES", "ITIETF", "ITIETF.NS", "itbees", "NIFTYBEES", "GOLDBEES", "MASPTOP50", "SOMEETF", "NIFTY1ETF",
        "LIQUIDPLUS", "SETFNIF50", "GSEC10YEAR", "GOLDCASE", "SILVERADD", "BANKBETA", "MONQ50",
    ])
    def test_etfs_and_funds_are_detected(self, sym):
        assert sf.is_etf_symbol(sym) is True

    @pytest.mark.parametrize("sym", [
        "RELIANCE", "INFY", "TCS", "HDFCBANK", "TATASTEEL", "SUNPHARMA", "COALINDIA", "NESTLEIND", "SAIL", "BETA",
        "HDFCAMC", "KPITTECH", "UCOBANK", "", None,
    ])
    def test_stocks_are_not(self, sym):
        assert sf.is_etf_symbol(sym) is False

    def test_extra_symbols_from_both_env_names(self, monkeypatch):
        assert sf.is_etf_symbol("ABCFUND") is False
        monkeypatch.setenv("CANDIDATE_ETF_SYMBOLS", " abcfund ")
        monkeypatch.setenv("WATCHLIST_ETF_SYMBOLS", "xyzfund.ns")
        assert sf.is_etf_symbol("ABCFUND") is True
        assert sf.is_etf_symbol("XYZFUND") is True

    def test_watchlist_helper_delegates_to_the_shared_test(self):
        for sym in ("ITIETF", "MAFANG", "RELIANCE", None):
            assert entry._wl_is_etf_symbol(sym) is sf.is_etf_symbol(sym)

    def test_drop_etf_symbols_keeps_order_and_non_strings(self):
        kept, dropped = sf.drop_etf_symbols(["RELIANCE", "ITBEES", "INFY", "ITIETF", 7])
        assert kept == ["RELIANCE", "INFY", 7] and dropped == ["ITBEES", "ITIETF"]

    def test_switch(self, monkeypatch):
        assert sf.candidate_etf_filter_on() is True
        monkeypatch.setenv("CANDIDATE_ETF_FILTER", "0")
        assert sf.candidate_etf_filter_on() is False

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(sf, "_etf_extras", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert sf.is_etf_symbol("RELIANCE") is False


# ── E2: both candidate tracks drop ETFs before any fetch ────────────────────────────────────────────────────

def _no_restricted(monkeypatch):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda db: set())


async def _noop_prefetch(client, symbols):
    return None


class TestEtfDroppedBeforeAnyFetch:
    def test_standard_track(self, db, monkeypatch, caplog):
        _no_restricted(monkeypatch)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [
                    {"symbol": "ITBEES", "decision": "BUY NOW", "score": 90.0, "price": 40.0},
                    {"symbol": "INFY", "decision": "BUY NOW", "score": 80.0, "price": 100.0},
                ]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)
        analysed, mcap_asked = [], []

        async def fake_mtf(client, symbol, quote=None):
            analysed.append(symbol)
            return {"reject_reason": None, "bullish_count": 5.0, "tf_returns": {}, "atr_pct": None, "market_note": ""}

        async def fake_mcap(client, symbol):
            mcap_asked.append(symbol)
            return 5000.0
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)
        monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)

        with caplog.at_level("INFO"):
            inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert analysed == ["INFY"] and mcap_asked == ["INFY"]
        assert inserted == 1 and [r.symbol for r in db.query(models.TradeCandidate).all()] == ["INFY"]
        assert "ITBEES" in seen            # still "handled": the volume-shock track must not re-propose it either
        assert any("dropped 1 ETF/fund" in m for m in caplog.messages)

    def test_standard_track_switch_off_keeps_etfs(self, db, monkeypatch):
        _no_restricted(monkeypatch)
        monkeypatch.setenv("CANDIDATE_ETF_FILTER", "0")

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [{"symbol": "ITBEES", "decision": "BUY NOW", "score": 90.0, "price": 40.0}]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)
        analysed = []

        async def fake_mtf(client, symbol, quote=None):
            analysed.append(symbol)
            return {"reject_reason": "x", "bullish_count": 0.0, "tf_returns": {}, "atr_pct": None}
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)
        run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert analysed == ["ITBEES"]

    def test_volume_shock_track(self, db, monkeypatch, caplog):
        _no_restricted(monkeypatch)

        async def fake_universe(client):
            return ["ITIETF", "ITBEES"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)
        analysed = []

        async def fake_vs(client, symbol, quote=None):
            analysed.append(symbol)
            return {"reject_reason": "x"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        with caplog.at_level("INFO"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0 and analysed == []
        assert any("volume_shock dropped 2 ETF/fund" in m for m in caplog.messages)

    def test_volume_shock_mixed_list_only_analyses_stocks(self, db, monkeypatch):
        _no_restricted(monkeypatch)

        async def fake_universe(client):
            return ["ITBEES", "ZOMATO"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)
        analysed = []

        async def fake_vs(client, symbol, quote=None):
            analysed.append(symbol)
            return {"reject_reason": "x"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)
        run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert analysed == ["ZOMATO"]
