"""Round 687 (E709): scripts/fno_sizing_whatif.py - the same lot rule as fno/sizing.py, applied to recorded trades."""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fno_sizing_whatif as w

T = {"day": "2026-11-20", "ts": 1.0, "premium": 100.0, "lot": 65, "risk_per_lot": 1625.0, "net_per_lot": 1000.0, "gross_per_lot": 1050.0}   # 100 x 65 x 25 %


def test_lots_follow_the_risk_budget_the_cash_share_and_the_cap():
    assert w.lots_for(T, 500000.0, 1.0, 5) == 3          # 5,000 / 1,625 = 3.07
    assert w.lots_for(T, 500000.0, 1.0, 2) == 2          # capped by max lots
    assert w.lots_for(T, 100000.0, 5.0, 9) == 3          # cash: 35,000 / 6,500 = 5.4; risk: 5,000 / 1,625 = 3.07
    assert w.lots_for(T, 100000.0, 20.0, 9) == 5         # the 35 % cash share binds
    assert w.lots_for(T, 50000.0, 1.0, 5) == 0           # 500 < 1,625: not even one lot
    assert w.lots_for({**T, "risk_per_lot": 0.0}, 500000.0, 1.0, 5) == 0


def test_scenario_skips_a_trade_that_does_not_fit_and_scales_the_rest():
    loss = {**T, "ts": 2.0, "net_per_lot": -600.0}
    s = w.scenario([T, loss], 500000.0, 1.0, 3)
    assert s["taken"] == 2 and s["skipped"] == 0 and s["net"] == 3 * 1000.0 + 3 * -600.0
    assert s["max_drawdown"] == -1800.0 and s["worst_trade"] == -1800.0
    assert w.scenario([T, loss], 50000.0, 1.0, 3)["taken"] == 0


def test_load_reads_closed_trades_of_the_mode_only(tmp_path):
    f = str(tmp_path / "fno.db")
    con = sqlite3.connect(f)
    con.execute("create table positions (id integer primary key autoincrement, mode text, underlying text, opt_type text, status text, opened_ts real, closed_ts real, day text, data text)")
    d = {"entry_premium": 100.0, "lot_size": 65, "lots": 2, "sl_price": 75.0, "net_pnl": 2000.0, "gross_pnl": 2100.0}
    con.execute("insert into positions(mode,underlying,opt_type,status,opened_ts,day,data) values('DEMO','NIFTY','CE','CLOSED',1,'2099-01-01',?)", (json.dumps(d),))
    con.execute("insert into positions(mode,underlying,opt_type,status,opened_ts,day,data) values('DEMO','NIFTY','CE','OPEN',2,'2099-01-01',?)", (json.dumps(d),))
    con.execute("insert into positions(mode,underlying,opt_type,status,opened_ts,day,data) values('REAL','NIFTY','CE','CLOSED',3,'2099-01-01',?)", (json.dumps(d),))
    con.commit()
    rows = w.load(f, "DEMO", 99999)
    assert len(rows) == 1 and rows[0]["net_per_lot"] == 1000.0 and rows[0]["risk_per_lot"] == 25.0 * 65


def test_table_has_a_row_per_setting():
    text = w.table([T], (100000.0, 500000.0), (1.0, 2.0), (2, 5))
    assert len(text.splitlines()) == 1 + 2 * 2 * 2
