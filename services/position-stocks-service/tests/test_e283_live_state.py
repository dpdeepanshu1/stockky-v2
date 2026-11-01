"""E283: GET /live/state (orders/live_state.py): four read-only parts, each isolated. No database, no market data."""
import re
import sys
import types
from pathlib import Path

from orders import live_state as ls

ROOT = Path(__file__).resolve().parent.parent


def test_snapshot_has_the_four_parts_and_one_failure_does_not_hide_the_others(monkeypatch):
    monkeypatch.setattr(ls, "order_rate_part", lambda: {"enabled": True, "budget_per_s": 4})
    monkeypatch.setattr(ls, "momentum_part", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ls, "heat_part", lambda db: {"peer_open_risk": 12.5})
    monkeypatch.setattr(ls, "ladder_part", lambda db: {"enabled": True, "stages": {"S1_CATALYST_UP|LONG": 1}})
    out = ls.snapshot(object())
    assert set(out) == {"order_rate", "momentum", "shared_heat", "ladder"}
    assert out["order_rate"] == {"ok": True, "enabled": True, "budget_per_s": 4}
    assert out["momentum"]["ok"] is False and out["momentum"]["error"].startswith("RuntimeError: boom")
    assert out["shared_heat"]["ok"] is True and out["shared_heat"]["peer_open_risk"] == 12.5
    assert out["ladder"]["stages"]["S1_CATALYST_UP|LONG"] == 1


def test_part_wraps_non_dict_values_and_cuts_long_errors():
    assert ls._part(lambda: 5) == {"ok": True, "value": 5}
    e = ls._part(lambda: (_ for _ in ()).throw(ValueError("x" * 500)))
    assert e["ok"] is False and len(e["error"]) <= 140


def test_heat_part_reads_the_peer_and_the_limit(monkeypatch):
    fake = types.ModuleType("capital.shared_heat")
    fake.get_peer = lambda db, age: (1234.567, 7288.0, 12.34)
    fake.get_peer_sectors = lambda db, age: {"Banks:LONG": 90.0}
    import capital
    monkeypatch.setitem(sys.modules, "capital.shared_heat", fake)
    monkeypatch.setattr(capital, "shared_heat", fake, raising=False)
    out = ls.heat_part(object())
    assert out["peer_open_risk"] == 1234.57 and out["peer_capital_base"] == 7288.0 and out["peer_age_s"] == 12.3
    assert out["limit_pct"] == 5.0 and out["peer_sector_risk"] == {"Banks:LONG": 90.0}


def test_ladder_part_lists_every_setup_and_side(monkeypatch):
    fake = types.ModuleType("orders.risk_stage")
    fake.stage_for = lambda db, sid, side: 2 if (sid, side) == ("S4_CATALYST_DOWN", "SHORT") else 0
    fake.enabled = lambda: True
    fake.short_cap_for = lambda db, sid=None: 3
    import orders
    monkeypatch.setitem(sys.modules, "orders.risk_stage", fake)
    monkeypatch.setattr(orders, "risk_stage", fake, raising=False)
    out = ls.ladder_part(object())
    assert len(out["stages"]) == 6 and out["stages"]["S4_CATALYST_DOWN|SHORT"] == 2 and out["stages"]["S1_CATALYST_UP|LONG"] == 0
    assert out["enabled"] is True and out["short_cap"] == 3


def test_order_rate_part_is_the_real_snapshot():
    out = ls.order_rate_part()
    assert "budget_per_s" in out and "enabled" in out


def test_route_is_admin_only_read_only_and_listed_on_the_calibration_page():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r'@app\.get\("/live/state"\)\ndef live_state_route\((.*?)\):', main, re.S)
    assert m and "require_admin" in m.group(1) and "get_db" in m.group(1)
    assert '@app.post("/live/state")' not in main
    probes = (ROOT.parent.parent / "frontend" / "src" / "statusProbes.ts").read_text(encoding="utf-8")
    assert probes.count('path: "/live/state"') == 2
