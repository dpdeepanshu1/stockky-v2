"""E269: the short order-flow tape gate is wired into the shared short plan. Static, runs anywhere (no SQLAlchemy, no network)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SE = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
SL = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")


def _plan_body():
    return SE[SE.index("def plan_short("):]


def test_the_tape_check_lives_in_the_shared_plan_so_shadow_and_live_refuse_alike():
    body = _plan_body()
    assert 'flow_gate.entry_check(sym, "SHORT", None)' in body
    assert body.index("short_gates.evaluate(") < body.index("flow_gate.entry_check(") < body.index("compute_levels(")
    assert '"SHORT_ORDER_FLOW"' in body
    assert '"tape_json": _tape_json' in body
    assert SE.count("flow_gate.entry_check(") == 1, "one call site: the plan, not the shadow row and the live entry separately"


def test_live_entry_records_the_tape_without_placing_or_blocking_anything_new():
    assert 'flow_gate.merge_tape(pos.flow_json, plan.get("tape_json"))' in SL
    assert "entry_check(" not in SL, "short_live must not ask the tape itself; the plan already did"
    code = "\n".join(l for l in SL.splitlines() if not l.strip().startswith("#"))
    assert code.count("place_super_order(") == 1
