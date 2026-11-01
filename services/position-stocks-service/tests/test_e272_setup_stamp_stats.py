"""E272: setup stamp and setup_stats. Plain-function runner: python3 tests/test_e272_setup_stamp_stats.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import setup_stamp, setup_stats


def test_time_slot_boundaries():
    d = lambda h, m: datetime(2026, 11, 2, h, m)
    assert setup_stamp.time_slot(d(9, 21)) == "0920"
    assert setup_stamp.time_slot(d(9, 29)) == "0920"
    assert setup_stamp.time_slot(d(9, 30)) == "0930"
    assert setup_stamp.time_slot(d(9, 59)) == "0930"
    assert setup_stamp.time_slot(d(10, 0)) == "1000"
    assert setup_stamp.time_slot(d(10, 30)) == "later"
    assert setup_stamp.time_slot(None) == "later"


def test_attach_keeps_other_keys_and_first_reason():
    st = setup_stamp.build(trigger="shortlist", size_mult=0.5, now=datetime(2026, 11, 2, 9, 40), penalties={"vwap": 0.2})
    out = setup_stamp.attach('{"tape":{"x":1}}', st)
    d = json.loads(out)
    assert d["tape"] == {"x": 1} and d["setup"]["id"] == setup_stamp.LEGACY_ID and d["setup"]["slot"] == "0930"
    again = setup_stamp.attach(out, setup_stamp.build("S1_CATALYST_UP"))
    assert json.loads(again)["setup"]["id"] == setup_stamp.LEGACY_ID      # first reason wins


def test_attach_never_raises_on_garbage():
    assert setup_stamp.attach("not json", {"id": "X"}) is not None
    assert setup_stamp.read("not json") is None and setup_stamp.read(None) is None
    assert json.loads(setup_stamp.attach(None, {"id": "X"}))["setup"]["id"] == "X"


def _row(setup, pnl, day, slot="0930", entry=100.0, stop=99.0, qty=10):
    st = setup_stamp.build(setup, now=datetime(2026, 11, 2, 9, 40) if slot == "0930" else datetime(2026, 11, 2, 11, 0))
    return {"flow_json": json.dumps({"setup": st}), "net_pnl": pnl, "entry_price": entry, "stop_price": stop, "quantity": qty, "opened_at": day}


def test_r_is_pnl_over_rupees_at_risk():
    assert abs(setup_stats.trade_r(_row("A", 20.0, "d")) - 2.0) < 1e-9       # risk = 1.0 x 10 = 10
    assert setup_stats.trade_r({"net_pnl": 5, "entry_price": 100, "stop_price": 100, "quantity": 1}) is None


def test_under_30_trades_never_gives_a_verdict():
    rows = [_row("A", 10.0, f"2026-10-{i % 28 + 1:02d}") for i in range(29)]
    s = setup_stats.summarize(rows)
    assert s["A|ALL"]["verdict"] == "NOT_ENOUGH_DATA" and s["A|ALL"]["lower_bound_r"] is None and s["A|ALL"]["n"] == 29


def test_a_clearly_positive_cell_is_positive_and_a_noisy_one_is_not():
    good = [_row("G", 10.0 + (i % 3), f"2026-10-{i % 10 + 1:02d}") for i in range(40)]
    noisy = [_row("N", 15.0 if i % 2 else -15.0, f"2026-10-{i % 10 + 1:02d}") for i in range(40)]
    s = setup_stats.summarize(good + noisy)
    assert s["G|ALL"]["verdict"] == "POSITIVE" and s["G|ALL"]["lower_bound_r"] > 0
    assert s["N|ALL"]["verdict"] == "NOT_PROVEN"


def test_slots_are_split_and_unstamped_rows_ignored():
    rows = [_row("A", 5.0, "2026-10-01", "0930"), _row("A", -5.0, "2026-10-01", "later"), {"flow_json": "{}", "net_pnl": 1}]
    s = setup_stats.summarize(rows)
    assert set(s) == {"A|0930", "A|later", "A|ALL"} and s["A|ALL"]["n"] == 2 and s["A|ALL"]["wins"] == 1


def test_entry_stamps_before_the_tm_gate_and_main_runs_the_final_read():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "orders", "entry.py"), encoding="utf-8").read()
    assert src.index("setup_stamp.attach(") < src.index("tm_gate.entry_check(candidate.symbol")      # stamped before the flow_json is saved
    assert "SETUP_STAMP_ENABLED" in src
    m = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "_preopen.due_final()" in m and "_preopen.run_final()" in m


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
