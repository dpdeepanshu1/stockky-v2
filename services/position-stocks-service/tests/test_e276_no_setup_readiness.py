"""E276 round 72: setup_stats cells carry `days`, and scripts/no_setup_readiness.py applies the plan's NO_SETUP_MODE rule to them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

from orders import setup_stats as ss  # noqa: E402
import no_setup_readiness as N  # noqa: E402


def _row(sid, day, pnl):
    return {"flow_json": json.dumps({"setup": {"id": sid, "slot": "0930", "side": "LONG"}}), "net_pnl": pnl, "entry_price": 100.0, "stop_price": 99.0, "quantity": 10,
            "opened_at": f"2026-10-{day:02d}T10:00:00"}


def test_cells_count_the_sessions_they_traded_in():
    rows = [_row("S2_RS_BREAKOUT", d, 5.0) for d in (1, 1, 2, 5)] + [_row("LEGACY_SHORTLIST", 3, -4.0)]
    out = ss.summarize(rows)
    assert out["S2_RS_BREAKOUT|ALL"]["days"] == 3 and out["S2_RS_BREAKOUT|ALL"]["n"] == 4
    assert out["LEGACY_SHORTLIST|ALL"]["days"] == 1


def _cell(n, avg, days, r_n=None):
    return {"n": n, "r_n": n if r_n is None else r_n, "avg_r": avg, "days": days}


def test_ready_only_when_every_part_of_the_plans_rule_holds():
    good = {"LEGACY_SHORTLIST|ALL": _cell(12, -0.3, 8), "S1_CATALYST_UP|ALL": _cell(25, 0.1, 10)}
    assert N.assess(good)["verdict"] == "READY"
    assert N.assess({**good, "S1_CATALYST_UP|ALL": _cell(25, 0.1, 9)})["verdict"] == "NOT_READY"                        # 9 sessions
    assert N.assess({"LEGACY_SHORTLIST|ALL": _cell(12, -0.3, 8), "S1_CATALYST_UP|ALL": _cell(10, 0.1, 10)})["verdict"] == "NOT_READY"   # 22 entries
    assert N.assess({**good, "LEGACY_SHORTLIST|ALL": _cell(12, 0.5, 8)})["verdict"] == "NOT_READY"                      # matched do worse than shortlist
    assert N.assess({"LEGACY_SHORTLIST|ALL": _cell(30, -0.3, 10), "S1_CATALYST_UP|ALL": _cell(10, 0.1, 10)})["verdict"] == "NOT_READY"   # 75 % unmatched
    assert N.assess({"LEGACY_SHORTLIST|ALL": _cell(12, -0.3, 8), "S1_CATALYST_UP|ALL": _cell(25, None, 10, r_n=0)})["verdict"] == "NOT_READY"   # no R known
    assert N.assess({})["verdict"] == "NOT_READY"


def test_slot_rows_are_not_double_counted_and_a_losing_match_is_flagged():
    cells = {"LEGACY_SHORTLIST|ALL": _cell(12, -0.5, 8), "S1_CATALYST_UP|ALL": _cell(25, -0.1, 10), "S1_CATALYST_UP|0920": _cell(25, -0.1, 10)}
    r = N.assess(cells)
    assert r["matched_n"] == 25 and r["verdict"] == "READY" and r["notes"]


def test_main_reads_a_saved_answer_and_a_bad_file_is_exit_2():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "ok.json").write_text(json.dumps({"ok": True, "cells": {"LEGACY_SHORTLIST|ALL": _cell(12, -0.3, 8), "S1_CATALYST_UP|ALL": _cell(25, 0.1, 10)}}))
    (d / "bad.json").write_text(json.dumps({"ok": False, "cells": {}}))
    assert N.main(["--file", str(d / "ok.json")]) == 0
    assert N.main(["--file", str(d / "bad.json")]) == 2
    assert N.main([]) == 2
