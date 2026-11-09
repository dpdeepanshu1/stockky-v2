"""F&O round 1: dhan_data/options.py - scrip-master option rows, chain normalisation, expiry list. Offline, no httpx needed.
Run from services/market-data-service:   python3 -m pytest tests/test_fno_options_core.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dhan_data import options

COMPACT = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG\n"
    "NSE,D,50001,OPTIDX,0,NIFTY-Nov2026-24500-CE,65,NIFTY 17 NOV 24500 CALL,2026-11-17 14:30:00,24500.0,CE,5.0,W\n"
    "NSE,D,50002,OPTIDX,0,NIFTY-Nov2026-24500-PE,65,NIFTY 17 NOV 24500 PUT,2026-11-17 14:30:00,24500.0,PE,5.0,W\n"
    "NSE,D,50003,OPTIDX,0,NIFTY-Nov2026-24600-CE,65,NIFTY 24 NOV 24600 CALL,2026-11-24 14:30:00,24600.0,CE,5.0,M\n"
    "BSE,D,60001,OPTIDX,0,SENSEX-Nov2026-80000-CE,20,SENSEX 19 NOV 80000 CALL,2026-11-19 15:30:00,80000.0,CE,5.0,W\n"
    "NSE,D,70001,OPTSTK,0,RELIANCE-Nov2026-1500-CE,500,x,2026-11-24 14:30:00,1500.0,CE,5.0,M\n"
    "NSE,D,70002,OPTIDX,0,BANKNIFTY-Nov2026-55000-CE,30,x,2026-11-24 14:30:00,55000.0,CE,5.0,M\n"
    "NSE,D,abc,OPTIDX,0,NIFTY-Nov2026-24700-CE,65,x,2026-11-24 14:30:00,24700.0,CE,5.0,M\n"
    "NSE,D,50004,OPTIDX,0,NIFTY-Nov2026-24500-CE,65,dup,2026-11-17 14:30:00,24500.0,CE,5.0,W\n"
    "NSE,E,2885,EQUITY,0,RELIANCE,1,x,,0,,5.0,\n"
)
DETAILED = (
    "EXCH_ID,SEGMENT,SECURITY_ID,INSTRUMENT,UNDERLYING_SYMBOL,SYMBOL_NAME,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG\n"
    "NSE,D,51001,OPTIDX,NIFTY,NIFTY-Dec2026-25000-PE,65,17-12-2026,25000,PE,0.05,M\n"
)


def setup_function(_f):
    options._set_for_tests([])


def test_parse_compact_keeps_only_nifty_sensex_index_options():
    rows = options.parse_option_rows(COMPACT)
    syms = sorted(c.symbol for c in rows)
    assert syms == ["NIFTY-Nov2026-24500-CE", "NIFTY-Nov2026-24500-PE", "NIFTY-Nov2026-24600-CE", "SENSEX-Nov2026-80000-CE"]


def test_contract_fields_and_segments():
    rows = {c.symbol: c for c in options.parse_option_rows(COMPACT)}
    n = rows["NIFTY-Nov2026-24500-CE"]
    assert (n.underlying, n.segment, n.security_id, n.lot_size, n.expiry, n.strike, n.opt_type, n.expiry_flag) == ("NIFTY", "NSE_FNO", 50001, 65, "2026-11-17", 24500.0, "CE", "W")
    assert n.tick_size == 0.05                      # 5.0 paise-style tick is read as Rs 0.05
    s = rows["SENSEX-Nov2026-80000-CE"]
    assert (s.segment, s.lot_size, s.exchange) == ("BSE_FNO", 20, "BSE")


def test_duplicate_rows_first_wins():
    rows = [c for c in options.parse_option_rows(COMPACT) if c.symbol == "NIFTY-Nov2026-24500-CE"]
    assert len(rows) == 1 and rows[0].security_id == 50001


def test_detailed_layout_and_dd_mm_yyyy_expiry():
    rows = options.parse_option_rows(DETAILED)
    assert len(rows) == 1 and rows[0].expiry == "2026-12-17" and rows[0].tick_size == 0.05 and rows[0].opt_type == "PE"


def test_garbage_input_is_empty_not_error():
    assert options.parse_option_rows("") == []
    assert options.parse_option_rows("a,b\n1,2\n") == []
    assert options.ingest("nonsense") == 0


def test_ingest_and_lookup():
    from datetime import date
    assert options.ingest(COMPACT) == 4
    assert options.list_expiries("NIFTY", today=date(2026, 11, 7)) == ["2026-11-17", "2026-11-24"]
    assert options.list_expiries("NIFTY", today=date(2026, 11, 18)) == ["2026-11-24"]      # a past expiry drops out
    assert options.list_expiries("SENSEX", today=date(2026, 11, 7)) == ["2026-11-19"]
    c = options.find_contract("nifty", "2026-11-17", 24500, "pe")
    assert c is not None and c.security_id == 50002
    st = options.status()
    assert st["contracts"] == 4 and st["lot_sizes"]["NIFTY"] == [65] and st["lot_sizes"]["SENSEX"] == [20]


def test_failed_reingest_keeps_old_map():
    options.ingest(COMPACT)
    assert options.ingest("garbage") == 0
    assert options.status()["contracts"] == 4


BODY = {"status": "success", "data": {"last_price": 24512.35, "oc": {
    "24600.000000": {"ce": {"security_id": 50003, "last_price": 80.5, "implied_volatility": 13.2, "greeks": {"delta": 0.41, "theta": -9.1, "gamma": 0.002, "vega": 8.0},
                            "oi": 120000, "previous_oi": 100000, "volume": 5000, "previous_volume": 4000, "previous_close_price": 75.0, "average_price": 79.0,
                            "top_bid_price": 80.4, "top_bid_quantity": 650, "top_ask_price": 80.6, "top_ask_quantity": 975},
                     "pe": {"security_id": 50004, "last_price": "bad"}},
    "24500.000000": {"ce": {"security_id": 50001, "last_price": 130.0, "oi": 90000}, "pe": None},
    "x": {"ce": {}, "pe": {}},
}}}


def test_normalize_chain_sorts_strikes_and_cleans_numbers():
    n = options.normalize_chain(BODY, "nifty", "2026-11-17")
    assert n["underlying"] == "NIFTY" and n["expiry"] == "2026-11-17" and n["spot"] == 24512.35
    assert [r["strike"] for r in n["strikes"]] == [24500.0, 24600.0]       # "x" skipped, sorted ascending
    r = n["strikes"][1]
    assert r["ce"]["ltp"] == 80.5 and r["ce"]["delta"] == 0.41 and r["ce"]["prev_oi"] == 100000 and r["ce"]["ask_qty"] == 975
    assert r["pe"]["ltp"] is None                                           # "bad" -> None, not an exception
    assert n["strikes"][0]["pe"] is None


def test_normalize_chain_handles_empty_and_wrong_shapes():
    for b in (None, {}, {"data": None}, {"data": {"oc": []}}, "x", []):
        n = options.normalize_chain(b, "SENSEX", "2026-11-19")
        assert n["strikes"] == [] and n["spot"] is None


def test_unsupported_underlying_raises_no_data():
    import pytest
    from dhan_data.errors import DhanNoDataError
    with pytest.raises(DhanNoDataError):
        options._underlying_ident("BANKNIFTY")
