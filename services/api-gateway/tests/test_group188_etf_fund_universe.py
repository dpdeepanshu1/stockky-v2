"""
group188 (item 9): the gateway's universe builder also drops liquid / gilt funds and ETF names that carry no
"ETF" word (BANKBETA, MONQ50, LIQUIDPLUS, SILVERADD, GSEC10YEAR, AONESILVER, GROWWMETAL ...), without touching real
companies with similar names.

Run from services/api-gateway:
    python3 -m pytest tests/test_group188_etf_fund_universe.py -q
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as gw

ETFS = ["ALPL30IETF", "BANKBETA", "COMMOIETF", "NV20IETF", "MONQ50", "LIQUIDPLUS", "SILVERADD", "GSEC10YEAR",
        "AONESILVER", "GROWWMETAL", "HDFCLIQUID", "LIQUIDSBI", "SBILIQETF", "NIFTYBEES", "NIFTY1", "GOLDCASE"]
STOCKS = ["RELIANCE", "TCS", "SILVERTUC", "GOLDIAM", "KRISHNADEF", "AJOONI", "PRECOT", "TARACHAND", "MACPOWER",
          "IRISDOREME", "HFCL", "REDINGTON", "SBIN"]


@pytest.mark.parametrize("sym", ETFS)
def test_etf_names_not_in_universe(sym):
    assert gw._clean_equity_symbol(sym) is None


@pytest.mark.parametrize("sym", STOCKS)
def test_real_stocks_stay(sym):
    assert gw._clean_equity_symbol(sym) == sym


def test_filter_equities_drops_them_in_order():
    assert gw._filter_equities(["TCS", "BANKBETA", "SBIN", "LIQUIDPLUS", "HFCL"]) == ["TCS", "SBIN", "HFCL"]


def test_news_symbol_extraction_uses_same_pattern():
    assert gw._NEWS_ETF_INDEX_SYMBOL_RE.search("GSEC10YEAR")
    assert not gw._NEWS_ETF_INDEX_SYMBOL_RE.search("HFCL")
