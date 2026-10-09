"""group278: ANALYSIS_DIRECT_YFINANCE_FALLBACK=0 stops technical history from calling yfinance itself.
Run from services/analysis-intelligence-service:  python3 -m pytest tests/test_group278_direct_yf_switch.py -v"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from technical import main as tech


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ANALYSIS_DIRECT_YFINANCE_FALLBACK", raising=False)
    monkeypatch.setenv("WAKE_PINGS", "0")


def _frame(n=30):
    return pd.DataFrame({"Open": [1.0] * n, "High": [1.0] * n, "Low": [1.0] * n, "Close": [1.0] * n, "Volume": [1] * n},
                        index=pd.date_range("2026-08-01", periods=n))


@pytest.mark.parametrize("raw,want", [(None, True), ("", True), ("1", True), ("0", False), ("off", False), ("No", False)])
def test_switch_values(monkeypatch, raw, want):
    if raw is not None:
        monkeypatch.setenv("ANALYSIS_DIRECT_YFINANCE_FALLBACK", raw)
    assert tech._direct_yf_ok() is want


def test_default_still_reaches_yfinance_when_market_data_has_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(tech, "_fetch_history_from_market_data", lambda *a, **k: None)
    monkeypatch.setattr(tech, "_fetch_history_yfinance", lambda s: calls.append(s) or _frame())
    monkeypatch.setattr(tech, "_fetch_history_bhavcopy_hint", lambda s: None)
    df = tech._fetch_history("TESTCO")
    assert calls == ["TESTCO"] and df is not None


def test_switch_off_never_calls_yfinance(monkeypatch):
    monkeypatch.setenv("ANALYSIS_DIRECT_YFINANCE_FALLBACK", "0")
    calls = []
    monkeypatch.setattr(tech, "_fetch_history_from_market_data", lambda *a, **k: None)
    monkeypatch.setattr(tech, "_fetch_history_yfinance", lambda s: calls.append(s) or _frame())
    monkeypatch.setattr(tech, "_fetch_history_bhavcopy_hint", lambda s: None)
    assert tech._fetch_history("TESTCO") is None and calls == []


def test_market_data_answer_is_used_without_yfinance(monkeypatch):
    calls = []
    monkeypatch.setattr(tech, "_fetch_history_from_market_data", lambda *a, **k: _frame())
    monkeypatch.setattr(tech, "_fetch_history_yfinance", lambda s: calls.append(s) or _frame())
    assert tech._fetch_history("TESTCO") is not None and calls == []
