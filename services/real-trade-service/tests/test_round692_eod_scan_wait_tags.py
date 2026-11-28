"""Round 692 (E714): the two EOD-signal-scan WAIT rows written by execution/auto_pilot.py carry a gate_tag. Source level, no database."""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "execution", "auto_pilot.py"), encoding="utf-8").read()
ROOT = os.path.dirname(os.path.dirname(HERE))


def test_both_eod_scan_waits_are_tagged():
    for head, tag in (("EOD signal scan: market is closing", "eod_scan"), ("EOD signal scan: qualified for same-day entry", "eod_carryover")):
        i = SRC.index(head)
        assert f'gate_tag="{tag}"' in SRC[i - 400:i], tag
        assert len(tag) <= 32


def test_they_stay_out_of_the_daily_gate_read():
    line = [ln for ln in open(os.path.join(ROOT, "scripts", "gates_daily.sh"), encoding="utf-8").read().splitlines() if ln.startswith("GATES=")][0]
    assert "eod_scan" not in line and "eod_carryover" not in line
