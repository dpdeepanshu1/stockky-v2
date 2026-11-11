"""Round 247: F&O REAL sizing respects the capital already tied up in open positions; capital_board temp files are per thread."""
from __future__ import annotations

import threading

import pytest

import capital_board
from fno import paper, sizing, store as st
from fno.models import ContractRef
from fno.params import Params
from test_engine_paper import NOW, FakeProvider, engine, make_cfg
from test_real_partial import RB, real


def test_lots_in_room_arithmetic():
    assert sizing.lots_in_room(4, 100.0, 65, 100000.0, 0.0) == 4              # 6,500 a lot: room for 15, the request is 4
    assert sizing.lots_in_room(4, 100.0, 65, 20000.0, 0.0) == 3
    assert sizing.lots_in_room(4, 100.0, 65, 20000.0, 14000.0) == 0           # 6,000 free < one lot
    assert sizing.lots_in_room(4, 100.0, 65, 20000.0, 7000.0) == 2
    assert sizing.lots_in_room(2, 100.0, 65, 5000.0, 0.0) == 0
    assert sizing.lots_in_room(0, 100.0, 65, 1e6, 0.0) == 0 and sizing.lots_in_room(2, 0.0, 65, 1e6, 0.0) == 0
    assert sizing.lots_in_room(2, 100.0, 65, 1e6, -5.0) == 2                  # a negative "used" never adds room


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _open_real(db, premium, lots=1, lot=20):
    c = ContractRef("SENSEX", "2026-11-17", 24500, "CE", lot, None, "BSE_FNO", 0.05)       # round 389: a strike the fake SENSEX chain really has, so the position is priced (an unpriced open position now blocks every new REAL entry before the capital check)
    p = paper.open_position(c, lots, premium, NOW.timestamp(), Params(capital=500000.0), mode="REAL")
    db.insert_position(p)
    return p


def test_real_entry_is_refused_when_the_open_positions_already_hold_the_allotment(db):
    b = RB()
    e = real(db, b)
    _open_real(db, premium=24700.0, lots=1)                                    # 24,700 x 20 = Rs 4,94,000 of the Rs 5,00,000 the board gives F&O
    before = len(db.open_positions("REAL"))
    e.tick(NOW)
    assert len(db.open_positions("REAL")) == before and not b.sent             # nothing was sent to Dhan
    ev = [x for x in db.events("skip") if x.get("stage") == "capital_room"]
    assert ev and "free after the open positions" in ev[0]["reasons"][0]


def test_real_entry_without_other_positions_is_unchanged(db):
    b = RB()
    e = real(db, b)
    e.tick(NOW)
    assert db.open_positions("REAL") and not [x for x in db.events("skip") if x.get("stage") == "capital_room"]


def test_demo_entries_ignore_the_room_rule(db):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="DEMO")
    e = engine(db, FakeProvider(), cfg)
    db.insert_position(paper.open_position(ContractRef("SENSEX", "2026-11-17", 80000, "CE", 65, None, "BSE_FNO", 0.05), 1, 7600.0, NOW.timestamp(), Params(capital=500000.0), mode="REAL"))
    e.tick(NOW)
    assert not [x for x in db.events("skip") if x.get("stage") == "capital_room"]


def test_capital_board_temp_file_is_per_thread(tmp_path, monkeypatch):
    names = []
    real_replace = capital_board.os.replace

    gate = threading.Barrier(2, timeout=5)                                       # both writers are alive and between "write tmp" and "rename" at the same moment

    def spy(src, dst):
        names.append(src)
        gate.wait()
        return real_replace(src, dst)

    monkeypatch.setattr(capital_board.os, "replace", spy)

    def go():
        assert capital_board.write_json(str(tmp_path), "x.json", {"a": 1})

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(names) == 2 and names[0] != names[1]
    assert [f for f in __import__("os").listdir(tmp_path) if f.endswith(".tmp")] == []
