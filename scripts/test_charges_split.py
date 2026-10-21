"""group 480: scripts/charges_split.py. Pure; synthetic records."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges_split as cs


def rec(pid, price=100.0, qty=10, charges=2.0, held=0, gross=5.0, **kw):
    d = {"position_id": pid, "mode": "REAL", "entry_price": price, "quantity": qty, "charges": charges, "held_days": held,
         "gross_pnl": gross, "net_pnl": gross - (charges or 0), "charges_source": "ledger", "opened_at": "2026-10-05T04:00:00+00:00"}
    d.update(kw)
    return d


def test_num_and_buckets():
    assert cs.num("1.5") == 1.5 and cs.num(None) is None and cs.num(True) is None and cs.num(float("nan")) is None
    assert [cs.hold_group(rec(1, held=h)) for h in (0, 1, 3)] == ["same_day", "carried", "carried"]
    assert cs.hold_group({"held_days": None}) == "unknown"
    assert [cs.size_bucket(v) for v in (1000, 2000, 3000, None)] == ["<1500", "1500-3000", ">=3000", "unknown"]
    assert cs.entry_value({"entry_price": 100, "quantity": 5}) == 500 and cs.entry_value({}) is None


def test_split_same_day_vs_carried():
    recs = [rec(1, charges=2.0), rec(2, charges=4.0), rec(3, charges=16.8, held=1), rec(4, charges=None, charges_source=None)]
    t = cs.summarize(recs, cs.hold_group)
    assert t["same_day"]["trades"] == 3 and t["same_day"]["charges_total"] == 6.0 and t["same_day"]["no_charges"] == 1
    assert t["same_day"]["charges_per_trade"] == 3.0
    assert t["carried"]["charges_per_trade"] == 16.8 and t["carried"]["net"] == round(5.0 - 16.8, 2)


def test_estimated_charges_are_counted():
    t = cs.summarize([rec(1, charges_source="estimate"), rec(2)], cs.hold_group)
    assert t["same_day"]["estimated_charges"] == 1


def test_load_dedupes_filters_mode_and_since(tmp_path):
    a, b = tmp_path / "rt_a.json", tmp_path / "rt_b.json"
    a.write_text(json.dumps({"records": [rec(1), rec(2, mode="DEMO"), rec(3, opened_at="2026-09-01T04:00:00+00:00")]}))
    b.write_text("not json")
    got = cs.load([str(a), str(b)], "REAL", "2026-10-01")
    assert [r["position_id"] for r in got] == [1]


def test_cli_end_to_end(tmp_path, capsys):
    f = tmp_path / "rt.json"
    f.write_text(json.dumps({"records": [rec(1, charges=2.0), rec(2, charges=16.8, held=1)]}))
    out = tmp_path / "o.json"
    assert cs.main([str(f), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Carried trades pay Rs 16.80 each against Rs 2.00" in text
    assert json.load(open(out))["by_hold"]["carried"]["trades"] == 1
    assert cs.main([str(tmp_path / "none.json")]) == 1
