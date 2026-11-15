"""Round 559 (P7): BANKNIFTY in the chain viewer only. market-data can read its expiry list and option chain (index id from the scrip master, Dhan /optionchain) but it is NOT an engine
underlying: no contract of it is parsed, stored or found, so no order can be built from it. Offline; no httpx, no network.
Run from services/market-data-service:   python3 -m pytest tests/test_round559_view_only_index.py -q"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhan_data import client, options, scrip_master
from dhan_data.errors import DhanNoDataError

CSV = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG\n"
    "NSE,D,50001,OPTIDX,0,NIFTY-Nov2026-24500-CE,65,x,2026-11-17 14:30:00,24500.0,CE,5.0,W\n"
    "NSE,D,70001,OPTIDX,0,BANKNIFTY-Nov2026-55000-CE,30,x,2026-11-24 14:30:00,55000.0,CE,5.0,M\n"
    "NSE,D,70002,OPTIDX,0,BANKNIFTY-Nov2026-55000-PE,30,x,2026-11-24 14:30:00,55000.0,PE,5.0,M\n"
)


@pytest.fixture(autouse=True)
def _clean():
    options._set_for_tests([])
    scrip_master._set_for_tests({}, {scrip_master._norm_index("BANKNIFTY"): 25, scrip_master._norm_index("NIFTY"): 13})
    with options._cache_lock:
        options._cache.clear()
    yield
    options._set_for_tests([])
    scrip_master._set_for_tests({}, {})
    with options._cache_lock:
        options._cache.clear()


def test_view_only_list_is_exactly_banknifty_and_never_overlaps_the_engine_underlyings():
    assert set(options.VIEW_ONLY_UNDERLYINGS) == {"BANKNIFTY"}
    assert set(options.UNDERLYINGS) == {"NIFTY", "SENSEX"}
    assert not set(options.VIEW_ONLY_UNDERLYINGS) & set(options.UNDERLYINGS)


def test_no_banknifty_contract_is_ever_parsed_stored_found_or_counted():
    rows = options.parse_option_rows(CSV)
    assert [c.symbol for c in rows] == ["NIFTY-Nov2026-24500-CE"]
    options._set_for_tests(rows)
    assert options.find_contract("BANKNIFTY", "2026-11-24", 55000.0, "CE") is None
    assert options.contracts_for("BANKNIFTY", None) == []
    st = options.status()
    assert set(st["by_underlying"]) == {"NIFTY", "SENSEX"} and set(st["lot_sizes"]) == {"NIFTY", "SENSEX"}


def test_ident_resolves_banknifty_from_the_index_table_and_still_refuses_others():
    assert options._underlying_ident("BANKNIFTY") == ("IDX_I", 25)
    assert options._underlying_ident("banknifty") == ("IDX_I", 25)
    assert options._underlying_ident("NIFTY") == ("IDX_I", 13)
    for bad in ("FINNIFTY", "MIDCPNIFTY", "RELIANCE", "", None):
        with pytest.raises(DhanNoDataError):
            options._underlying_ident(bad)


def test_ident_says_when_the_index_id_is_not_in_the_scrip_master():
    scrip_master._set_for_tests({}, {})
    with pytest.raises(DhanNoDataError):
        options._underlying_ident("BANKNIFTY")


def _stub_post(monkeypatch, calls):
    def post(path, payload, *, limiter):
        calls.append((path, dict(payload)))
        if path == "optionchain/expirylist":
            return {"data": ["2026-11-24", "2026-12-29"]}
        return {"data": {"last_price": 55040.5, "oc": {"55000.000000": {"ce": {"last_price": 120.5, "oi": 1000}, "pe": {"last_price": 98.0, "oi": 1500}}}}}
    monkeypatch.setattr(client, "post", post)
    monkeypatch.setattr(client, "note_success", lambda: None)
    monkeypatch.setattr(client, "note_failure", lambda e: None)
    monkeypatch.setattr(options, "_chain_limiter", lambda: None)


def test_expiries_and_chain_use_the_banknifty_index_id_and_segment(monkeypatch):
    calls = []
    _stub_post(monkeypatch, calls)
    assert options.fetch_expiries("BANKNIFTY") == ["2026-11-24", "2026-12-29"]
    ch = options.fetch_chain("BANKNIFTY", "2026-11-24")
    assert ch["underlying"] == "BANKNIFTY" and ch["expiry"] == "2026-11-24" and ch["spot"] == 55040.5
    assert calls[0] == ("optionchain/expirylist", {"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I"})
    assert calls[1] == ("optionchain", {"UnderlyingScrip": 25, "UnderlyingSeg": "IDX_I", "Expiry": "2026-11-24"})


def test_view_only_chain_has_its_own_longer_cache_and_engine_chains_keep_the_short_one(monkeypatch):
    calls = []
    _stub_post(monkeypatch, calls)
    monkeypatch.delenv("DHAN_OPTIONCHAIN_VIEW_CACHE_S", raising=False)
    monkeypatch.delenv("DHAN_OPTIONCHAIN_CACHE_S", raising=False)
    t = [1000.0]
    monkeypatch.setattr(options.time, "monotonic", lambda: t[0])
    options.fetch_chain("BANKNIFTY", "2026-11-24")
    t[0] += 8.0
    options.fetch_chain("BANKNIFTY", "2026-11-24")
    assert [c[0] for c in calls].count("optionchain") == 1
    t[0] += 3.0
    options.fetch_chain("BANKNIFTY", "2026-11-24")
    assert [c[0] for c in calls].count("optionchain") == 2
    calls.clear()
    options.fetch_chain("NIFTY", "2026-11-17")
    t[0] += 4.0
    options.fetch_chain("NIFTY", "2026-11-17")
    assert [c[0] for c in calls].count("optionchain") == 2


def test_view_cache_has_a_floor_and_an_explicit_max_age_still_wins(monkeypatch):
    calls = []
    _stub_post(monkeypatch, calls)
    monkeypatch.setenv("DHAN_OPTIONCHAIN_VIEW_CACHE_S", "0")
    t = [500.0]
    monkeypatch.setattr(options.time, "monotonic", lambda: t[0])
    options.fetch_chain("BANKNIFTY", "2026-11-24")
    t[0] += 2.0
    options.fetch_chain("BANKNIFTY", "2026-11-24")
    assert [c[0] for c in calls].count("optionchain") == 1
    t[0] += 1.0
    options.fetch_chain("BANKNIFTY", "2026-11-24", max_age_s=0.5)
    assert [c[0] for c in calls].count("optionchain") == 2


def test_the_http_routes_pass_the_underlying_through_without_a_list_of_their_own():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    i = src.index('@app.get("/dhan/options/expiries")')
    block = src[i:src.index('@app.get("/depth/{symbol}")', i)]
    assert "VIEW_ONLY" not in block and '"BANKNIFTY"' not in block, "the routes stay generic: the list lives in dhan_data/options.py only"
