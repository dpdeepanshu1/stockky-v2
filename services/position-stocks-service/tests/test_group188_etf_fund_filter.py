"""
group188 (item 9): ETFs / index funds / liquid and gilt funds are left out of the WebSocket subscription (the
same name test as market-data-service). Conservative; real stocks with similar names must stay.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_group188_etf_fund_filter.py -q
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from feed import instrument_filter as f

# From the 2026-10-06 boot log (items 9 and 10) - all ETFs / funds by name.
ETFS = ["ALPL30IETF", "BANKBETA", "COMMOIETF", "NV20IETF", "MONQ50", "LIQUIDPLUS", "SILVERADD", "GSEC10YEAR",
        "AONESILVER", "GROWWMETAL", "HDFCLIQUID", "LIQUIDSBI", "SBILIQETF", "NIFTYBEES", "GOLDBEES", "SETFNIF50",
        "NIFTY1", "GOLDCASE", "MASPTOP50"]
# Real operating companies whose names sit close to the patterns.
STOCKS = ["RELIANCE", "TCS", "SILVERTUC", "GOLDIAM", "KRISHNADEF", "AJOONI", "PRECOT", "TARACHAND", "MACPOWER",
          "OSWALSEEDS", "IRISDOREME", "HFCL", "REDINGTON", "BETA", "SBIN", "COALINDIA", "FOCUS", "TECH", "CHEMICAL"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ETF_FUND_FILTER", raising=False)
    monkeypatch.delenv("ETF_FUND_EXTRA_SYMBOLS", raising=False)


@pytest.mark.parametrize("sym", ETFS)
def test_etf_names_recognised(sym):
    assert f.is_etf_or_fund(sym)
    assert f.is_etf_or_fund(sym.lower() + ".NS")


@pytest.mark.parametrize("sym", STOCKS)
def test_real_stocks_kept(sym):
    assert not f.is_etf_or_fund(sym)


def test_empty_and_none():
    assert not f.is_etf_or_fund("")
    assert not f.is_etf_or_fund(None)


def test_switch_off(monkeypatch):
    monkeypatch.setenv("ETF_FUND_FILTER", "0")
    assert not f.is_etf_or_fund("BANKBETA")


def test_extra_symbols(monkeypatch):
    assert not f.is_etf_or_fund("SOMEFUND")
    monkeypatch.setenv("ETF_FUND_EXTRA_SYMBOLS", "somefund, other")
    assert f.is_etf_or_fund("SOMEFUND.NS")


def test_drop_etfs_counts_and_keeps_input():
    src = {"TCS": "1", "BANKBETA": "2", "LIQUIDPLUS": "3", "SBIN": "4"}
    kept, n = f.drop_etfs(src)
    assert kept == {"TCS": "1", "SBIN": "4"} and n == 2
    assert len(src) == 4
