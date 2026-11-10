"""Round 297: (#7) net_positions sums rows per contract and keeps the segment; callers look up by (segment, id).
(#12 option 1) REAL positions open while no broker is loaded are announced at startup, in the journal and on the overview."""
from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest

from fno import execution, paper, store as st
from fno.execution import NetPositions, net_qty
from fno.models import ContractRef
from fno.params import Params
from test_engine_paper import NOW, FakeProvider, engine, make_cfg, real_engine, StatusBroker


class _R(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _broker(rows):
    return execution.DhanBroker("https://x", "c", "t", opener=lambda req, timeout=None: _R(json.dumps(rows).encode()))


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def test_two_rows_of_one_contract_are_added_not_overwritten():
    b = _broker([{"securityId": "111", "netQty": 65, "exchangeSegment": "NSE_FNO", "productType": "INTRADAY"},
                 {"securityId": "111", "netQty": 130, "exchangeSegment": "NSE_FNO", "productType": "NRML"}])
    n = b.net_positions()
    assert n == {"111": 195} and net_qty(n, "111", "NSE_FNO") == 195


def test_opposite_rows_of_one_contract_cancel_out():
    n = _broker([{"securityId": "111", "netQty": 65, "exchangeSegment": "NSE_FNO"}, {"securityId": "111", "netQty": -65, "exchangeSegment": "NSE_FNO"}]).net_positions()
    assert n == {} and net_qty(n, "111", "NSE_FNO") == 0


def test_the_same_id_on_nse_and_bse_stays_apart_when_the_segment_is_known():
    n = _broker([{"securityId": "500", "netQty": 65, "exchangeSegment": "NSE_FNO"}, {"securityId": "500", "netQty": 20, "exchangeSegment": "BSE_FNO"}]).net_positions()
    assert net_qty(n, "500", "NSE_FNO") == 65 and net_qty(n, "500", "BSE_FNO") == 20
    assert net_qty(n, "500", "MCX_FNO") == 0
    assert net_qty(n, "500") == 85                                      # no segment given: the id alone, summed


def test_net_qty_on_a_plain_dict_or_none():
    assert net_qty({"7": 5}, 7, "NSE_FNO") == 5 and net_qty(None, 7) == 0 and net_qty({}, 7, "NSE_FNO") == 0


def test_a_position_is_not_confirmed_by_another_segments_quantity(db):
    b = StatusBroker("TRADED", net=NetPositions())
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    other = "BSE_FNO" if p.segment != "BSE_FNO" else "NSE_FNO"
    n = NetPositions({str(p.security_id): p.qty})
    n.by_segment = {(other, str(p.security_id)): p.qty}                 # Dhan holds that id, but in the other exchange
    b.net = n
    r = e.reconcile(NOW + timedelta(seconds=120), force=True)
    assert r["ok"] is False and {d["segment"] for d in r["differences"]} == {p.segment, other}


def test_reconcile_with_segments_is_clean_when_both_agree(db):
    b = StatusBroker("TRADED", net=NetPositions())
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    n = NetPositions({str(p.security_id): p.qty})
    n.by_segment = {(p.segment, str(p.security_id)): p.qty}
    b.net = n
    assert e.reconcile(NOW + timedelta(seconds=120), force=True)["ok"] is True


def test_a_row_saved_without_a_segment_matches_the_only_segment_dhan_shows(db):
    b = StatusBroker("TRADED", net=NetPositions())
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    p.segment = ""
    db.update_position(p)
    n = NetPositions({str(p.security_id): p.qty})
    n.by_segment = {("NSE_FNO", str(p.security_id)): p.qty}
    b.net = n
    assert e.reconcile(NOW + timedelta(seconds=120), force=True)["ok"] is True


# ---- #12 option 1 ----------------------------------------------------------------------------------------------------------
def _open_real(db):
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 1, 100.0, NOW.timestamp(), Params(), mode="REAL")
    db.insert_position(p)
    return p


def test_no_broker_and_a_real_position_is_announced(db):
    _open_real(db)
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=None)
    assert len(e.warn_unmanaged_real(NOW, force=True)) == 1
    ev = [x for x in db.events() if x["kind"] == "unmanaged_real"]
    assert len(ev) == 1 and "ARM" in json.dumps(ev[0])


def test_nothing_is_said_when_there_is_no_real_position_or_a_broker_is_loaded(db):
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=None)
    assert e.warn_unmanaged_real(NOW, force=True) == [] and not [x for x in db.events() if x["kind"] == "unmanaged_real"]
    _open_real(db)
    e2 = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=StatusBroker("TRADED"))
    assert e2.warn_unmanaged_real(NOW, force=True) == []


def test_a_paper_position_does_not_trigger_it(db):
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    db.insert_position(paper.open_position(c, 1, 100.0, NOW.timestamp(), Params(), mode="DEMO"))
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=None)
    assert e.warn_unmanaged_real(NOW, force=True) == []


def test_the_tick_repeats_the_warning_only_every_fifteen_minutes(db):
    _open_real(db)
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=None)
    n = lambda: len([x for x in db.events() if x["kind"] == "unmanaged_real"])      # noqa: E731
    e.warn_unmanaged_real(NOW, force=True)
    e.warn_unmanaged_real(NOW + timedelta(minutes=5))
    assert n() == 1
    e.warn_unmanaged_real(NOW + timedelta(minutes=16))
    assert n() == 2


def test_tick_itself_raises_the_warning_and_still_runs(db):
    _open_real(db)
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=None)
    out = e.tick(NOW)
    assert "ts" in out and [x for x in db.events() if x["kind"] == "unmanaged_real"]


def test_main_wires_startup_and_overview():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    assert "engine.warn_unmanaged_real(force=True)" in src and '"unmanaged_real": [' in src
