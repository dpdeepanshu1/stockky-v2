"""E283: GET /live/state of Real Trade (execution/live_state.py): pool, heat and setup-stamp counts. No database."""
import json
import re
import sys
import types
from pathlib import Path

from execution import live_state as ls

ROOT = Path(__file__).resolve().parent.parent


def test_stamp_counts():
    rows = [json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930"}}), json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0945"}}),
            json.dumps({"setup": {"id": "LEGACY_SHORTLIST", "slot": "later"}}), json.dumps({"x": 1}), "not json", None, {"setup": {"id": "S2_RS_BREAKOUT"}}]
    out = ls.stamp_counts(rows)
    assert out["n"] == 7 and out["stamped"] == 4 and out["unstamped"] == 3
    assert out["by_setup"] == {"S1_CATALYST_UP": 2, "LEGACY_SHORTLIST": 1, "S2_RS_BREAKOUT": 1}
    assert out["by_slot"] == {"0930": 1, "0945": 1, "later": 1, "?": 1}
    assert ls.stamp_counts([]) == {"n": 0, "stamped": 0, "unstamped": 0, "by_setup": {}, "by_slot": {}}


def test_snapshot_isolates_a_failing_part(monkeypatch):
    monkeypatch.setattr(ls, "pool_part", lambda db: {"accounts": [{"mode": "REAL", "starting_capital": 7288.0}]})
    monkeypatch.setattr(ls, "heat_part", lambda db: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(ls, "stamp_part", lambda db, limit=300: {"n": 0})
    monkeypatch.setattr(ls, "order_rate_part", lambda: {"enabled": True, "working": True})
    out = ls.snapshot(object())
    assert set(out) == {"pool", "heat", "setup_stamp", "order_rate"}      # E285 round 87 added order_rate
    assert out["pool"]["ok"] is True and out["pool"]["accounts"][0]["mode"] == "REAL"
    assert out["heat"] == {"ok": False, "error": "RuntimeError: db down"}
    assert out["setup_stamp"]["ok"] is True


def test_heat_part_numbers(monkeypatch):
    fake = types.ModuleType("execution.heat_state")
    fake.own_heat = lambda db, mode: (100.456, 20.0)
    fake.peer_heat = lambda db, mode: (50.0, 7000.0)
    import execution
    monkeypatch.setitem(sys.modules, "execution.heat_state", fake)
    monkeypatch.setattr(execution, "heat_state", fake, raising=False)
    out = ls.heat_part(object())
    assert out["own_open_risk"] == 100.46 and out["own_gap_allowance"] == 20.0 and out["peer_open_risk"] == 50.0 and out["peer_capital_base"] == 7000.0


def test_route_is_admin_only_read_only():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r'@app\.get\("/live/state"\)\nasync def live_state_route\((.*?)\):', main, re.S)
    assert m and "require_admin" in m.group(1) and "get_db" in m.group(1)
    assert '@app.post("/live/state")' not in main
