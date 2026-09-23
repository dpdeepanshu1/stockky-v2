"""
tests/test_sector_signal.py  — session89, step 4
=================================================
Coverage target:
  market_context/sector_signal.py   17% → 100%   (64 statements)

Approach:
  - sector_bonus_for_symbol() is pure — no mocking needed, just flip
    config.US_SECTOR_SIGNAL_ENABLED / US_SECTOR_BONUS_CAP /
    US_SECTOR_BONUS_FULL_SCALE_PCT via monkeypatch and feed it
    sector_returns dicts directly.
  - refresh_us_sector_snapshot() imports load_snapshot/save_snapshot and
    ist_today_str *inside the function body* (not at module level), so
    they must be patched on their owning modules
    (resilience.local_cache.*, tz_utils.ist_today_str) rather than on
    market_context.sector_signal — patching the sector_signal name would
    silently no-op since the function re-imports fresh on every call.
  - _fetch_us_sector_returns() imports yfinance the same way (inside a
    try/except). We fake it out via unittest.mock.patch.dict on
    sys.modules: {"yfinance": None} forces the ImportError branch even
    though yfinance is actually installed; a MagicMock in its place
    drives the success/failure/per-ticker-exception branches.

Every branch below (including the exact expected numeric bonus values)
was hand-verified against the live module before being written into
assertions here, so the arithmetic is not guessed.

Run from services/real-trade-service:
    python3 -m pytest tests/test_sector_signal.py -v \
        --cov=market_context.sector_signal --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import market_context.sector_signal as ss

_FAKE_DB = object()


# ══════════════════════════════════════════════════════════════════════════════
# sector_bonus_for_symbol()  — pure function
# ══════════════════════════════════════════════════════════════════════════════

class TestSectorBonusForSymbol:
    def test_disabled_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", False)
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": 2.0}) == 0.0

    def test_empty_sector_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        assert ss.sector_bonus_for_symbol("TCS", {}) == 0.0

    def test_unmapped_symbol_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        assert ss.sector_bonus_for_symbol("NOTASYMBOL", {"XLK": 2.0}) == 0.0

    def test_sector_with_no_etf_mapping_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setitem(ss.NSE_SECTOR_MAP, "FAKESYM", "NOSUCHSECTOR")
        try:
            assert ss.sector_bonus_for_symbol("FAKESYM", {"XLK": 2.0}) == 0.0
        finally:
            del ss.NSE_SECTOR_MAP["FAKESYM"]

    def test_etf_missing_from_returns_dict_is_zero(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        # TCS -> IT -> XLK, but only XLF is present in the returns dict
        assert ss.sector_bonus_for_symbol("TCS", {"XLF": 2.0}) == 0.0

    def test_positive_proportional_bonus(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        # TCS -> IT -> XLK; pct = 0.75 (half of scale) -> (0.75/1.5)*6.0 = 3.0
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": 0.75}) == 3.0

    def test_negative_proportional_bonus(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": -0.75}) == -3.0

    def test_bonus_clamped_to_positive_cap(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": 10.0}) == 6.0

    def test_bonus_clamped_to_negative_cap(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": -10.0}) == -6.0

    def test_zero_scale_falls_back_to_minimum_scale(self, monkeypatch):
        # max(config.US_SECTOR_BONUS_FULL_SCALE_PCT, 0.01) guards div-by-zero
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 0.0)
        # scale floors at 0.01, so even a tiny pct saturates the cap immediately
        assert ss.sector_bonus_for_symbol("TCS", {"XLK": 1.0}) == 6.0

    def test_second_symbol_in_same_sector_maps_to_same_etf(self, monkeypatch):
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        # INFY is also IT -> XLK, same as TCS
        assert ss.sector_bonus_for_symbol("INFY", {"XLK": 0.75}) == 3.0

    def test_alias_sector_shares_etf(self, monkeypatch):
        # HDFCBANK -> BANK -> XLF ; BAJFINANCE -> FINANCIAL_SERVICES -> XLF
        # (two different NSE sector labels, same underlying ETF)
        monkeypatch.setattr(config, "US_SECTOR_SIGNAL_ENABLED", True)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_CAP", 6.0)
        monkeypatch.setattr(config, "US_SECTOR_BONUS_FULL_SCALE_PCT", 1.5)
        bank_bonus = ss.sector_bonus_for_symbol("HDFCBANK", {"XLF": 1.5})
        fin_bonus = ss.sector_bonus_for_symbol("BAJFINANCE", {"XLF": 1.5})
        assert bank_bonus == fin_bonus == 6.0


# ══════════════════════════════════════════════════════════════════════════════
# refresh_us_sector_snapshot()
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshUsSectorSnapshot:
    def test_load_snapshot_exception_falls_through_to_fetch(self):
        with patch("resilience.local_cache.load_snapshot", side_effect=RuntimeError("db down")), \
             patch("resilience.local_cache.save_snapshot") as save_mock, \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns",
                   return_value={"XLK": 1.0}):
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        assert result == {"XLK": 1.0}
        assert save_mock.called

    def test_cache_hit_same_day_skips_fetch(self):
        fake_snap = {"trading_date": "2026-09-23", "returns": {"XLK": 9.9}}
        with patch("resilience.local_cache.load_snapshot", return_value=fake_snap), \
             patch("resilience.local_cache.save_snapshot") as save_mock, \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns") as fetch_mock:
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        assert result == {"XLK": 9.9}
        assert not fetch_mock.called
        assert not save_mock.called

    def test_stale_trading_date_refetches(self):
        fake_snap = {"trading_date": "2026-09-22", "returns": {"XLK": 9.9}}
        with patch("resilience.local_cache.load_snapshot", return_value=fake_snap), \
             patch("resilience.local_cache.save_snapshot") as save_mock, \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns",
                   return_value={"XLF": 2.0}):
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        assert result == {"XLF": 2.0}
        assert save_mock.called

    def test_empty_cached_returns_refetches(self):
        # trading_date matches today but "returns" is falsy -> still refetch
        fake_snap = {"trading_date": "2026-09-23", "returns": {}}
        with patch("resilience.local_cache.load_snapshot", return_value=fake_snap), \
             patch("resilience.local_cache.save_snapshot"), \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns",
                   return_value={"XLE": 3.0}):
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        assert result == {"XLE": 3.0}

    def test_no_existing_snapshot_fetches_and_saves(self):
        with patch("resilience.local_cache.load_snapshot", return_value=None), \
             patch("resilience.local_cache.save_snapshot") as save_mock, \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns",
                   return_value={"XLY": 5.0}):
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        assert result == {"XLY": 5.0}
        assert save_mock.called
        saved_payload = save_mock.call_args[0][2]
        assert saved_payload["trading_date"] == "2026-09-23"
        assert saved_payload["returns"] == {"XLY": 5.0}
        assert "fetched_at" in saved_payload

    def test_save_snapshot_failure_is_swallowed(self):
        with patch("resilience.local_cache.load_snapshot", return_value=None), \
             patch("resilience.local_cache.save_snapshot", side_effect=RuntimeError("write fail")), \
             patch("tz_utils.ist_today_str", return_value="2026-09-23"), \
             patch("market_context.sector_signal._fetch_us_sector_returns",
                   return_value={"XLB": 4.0}):
            result = ss.refresh_us_sector_snapshot(_FAKE_DB)
        # save failed but the fetched returns are still handed back — never fatal
        assert result == {"XLB": 4.0}


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_us_sector_returns()
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchUsSectorReturns:
    def test_yfinance_not_installed_returns_empty(self):
        with patch.dict(sys.modules, {"yfinance": None}):
            result = ss._fetch_us_sector_returns()
        assert result == {}

    def test_yf_download_exception_returns_empty(self):
        fake_yf = MagicMock()
        fake_yf.download.side_effect = RuntimeError("network down")
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            result = ss._fetch_us_sector_returns()
        assert result == {}

    def test_multi_ticker_mixed_outcomes(self):
        """
        Exercises, in one pass, all four per-ticker branches:
          - normal 2-close series -> % change computed and rounded
          - <2 closes after dropna -> skipped (continue)
          - prev close <= 0 -> skipped (continue)
          - column access raises -> caught by inner except -> skipped
        """
        import pandas as pd

        tickers = sorted(set(ss.SECTOR_ETF_MAP.values()))
        assert len(tickers) > 1  # sanity: real map always exercises the multi-ticker branch

        good_t, short_t, nonpositive_t, raising_t, negative_t = tickers[:5]

        series_by_ticker = {
            good_t: pd.Series([100.0, 102.0]),          # +2.0%
            short_t: pd.Series([100.0]),                 # only 1 close -> skip
            nonpositive_t: pd.Series([0.0, 5.0]),         # prev <= 0 -> skip
            negative_t: pd.Series([50.0, 49.0]),          # -2.0%
        }

        def _col_getter(ticker):
            inner = MagicMock()
            if ticker == raising_t:
                inner.__getitem__ = MagicMock(side_effect=KeyError("Close"))
            else:
                series = series_by_ticker.get(ticker, pd.Series([10.0, 10.0]))
                inner.__getitem__ = MagicMock(
                    side_effect=lambda key, s=series: s if key == "Close" else (_ for _ in ()).throw(KeyError(key))
                )
            return inner

        fake_data = MagicMock()
        fake_data.__getitem__ = MagicMock(side_effect=_col_getter)

        fake_yf = MagicMock()
        fake_yf.download.return_value = fake_data

        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            result = ss._fetch_us_sector_returns()

        assert result[good_t] == 2.0
        assert short_t not in result
        assert nonpositive_t not in result
        assert raising_t not in result
        assert result[negative_t] == round((49.0 - 50.0) / 50.0 * 100.0, 3)

    def test_download_called_with_all_unique_etfs(self):
        fake_yf = MagicMock()
        fake_yf.download.side_effect = RuntimeError("stop before parsing")
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            ss._fetch_us_sector_returns()
        expected_tickers = " ".join(sorted(set(ss.SECTOR_ETF_MAP.values())))
        _, kwargs = fake_yf.download.call_args
        assert kwargs["tickers"] == expected_tickers
