"""E273 round 35: the slot size ladder - promote a cell with a positive lower bound, cut a cell that loses, leave thin cells alone.
Plain runner: python3 tests/test_e273_slot_ladder.py (service folder). Stdlib only; sqlalchemy / httpx stubbed when missing."""
from __future__ import annotations

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

import config  # noqa: E402
from orders import setup_engine, setup_live, slot_ladder  # noqa: E402


def row(setup_id, slot, r, day):
    """A closed trade with R = r (entry 100, stop 99, 10 shares -> risk Rs 10)."""
    return {"flow_json": json.dumps({"setup": {"id": setup_id, "slot": slot, "penalties": {}, "size_mult": 0.25}}), "realized_pnl": 10.0 * r,
            "entry_price": 100.0, "stop_price": 99.0, "quantity": 10, "opened_at": f"2026-10-{day:02d} 09:30:00"}


def rows(setup_id, slot, rs):
    return [row(setup_id, slot, r, 1 + (i % 8)) for i, r in enumerate(rs)]


WIN = [0.8, 1.2, 0.6, 1.0, -0.2, 0.9, 1.1, 0.7, 1.3, 0.5] * 4       # 40 trades, clearly positive
LOSE = [-0.9, -1.0, 0.2, -0.8, -1.1, -0.6, 0.1, -0.7, -1.0, -0.9] * 4  # 40 trades, clearly negative


def test_winning_cell_is_promoted_losing_cell_is_demoted_thin_cell_is_neither():
    data = rows("S1_CATALYST_UP", "0920", WIN) + rows("S2_RS_BREAKOUT", "0930", LOSE) + rows("S3_EXHAUSTION", "1000", WIN[:10])
    lad = slot_ladder.from_rows(data)
    assert lad["promoted"] == {"S1_CATALYST_UP|0920"} and lad["demoted"] == {"S2_RS_BREAKOUT|0930"}
    assert "S3_EXHAUSTION|1000" not in lad["promoted"] | lad["demoted"]            # 10 trades: NOT_ENOUGH_DATA
    assert not any(k.endswith("|ALL") for k in lad["promoted"] | lad["demoted"])


def test_legacy_stamps_and_garbage_never_enter_the_ladder():
    lad = slot_ladder.from_rows(rows("LEGACY_SHORTLIST", "later", LOSE) + [{"flow_json": "not json"}, None, {}])
    assert lad["promoted"] == set() and lad["demoted"] == set()
    assert slot_ladder.from_rows(None)["promoted"] == set()


def test_demote_factor_only_for_a_demoted_cell():
    lad = {"demoted": {"S2_RS_BREAKOUT|0930"}}
    assert slot_ladder.demote_factor("S2_RS_BREAKOUT", "0930", lad) == config.SLOT_LADDER_DEMOTE_FACTOR == 0.5
    assert slot_ladder.demote_factor("S2_RS_BREAKOUT", "1000", lad) == 1.0 and slot_ladder.demote_factor(None, "0930", lad) == 1.0
    assert slot_ladder.demote_factor("S2_RS_BREAKOUT", "0930", None) == 1.0 and slot_ladder.demote_factor("S2_RS_BREAKOUT", "0930", {}) == 1.0


def test_engine_sizes_a_promoted_cell_at_full_size_and_a_normal_cell_at_its_start():
    f = {"promoted": {"S1_CATALYST_UP|0920"}}
    assert setup_engine._size("S1_CATALYST_UP", "0920", f, 0.0) == 1.0
    assert setup_engine._size("S1_CATALYST_UP", "0920", {}, 0.0) == 0.25
    assert setup_engine._size("S2_RS_BREAKOUT", "0930", {}, 0.0) == 0.5
    assert setup_engine._size("S1_CATALYST_UP", "0920", f, 1.0) == 0.5            # penalties still apply on top of full size


def test_promoted_reaches_the_engine_facts_through_the_inputs():
    assert "promoted" in setup_live.INPUT_KEYS
    f = setup_live.facts_long(symbol="X", price=100.0, now=None, event_polarity=1, flow_json=None, quote=None, gap_pct=None, inputs={"promoted": {"A|0920"}})
    assert f.get("promoted") == {"A|0920"}


def test_get_is_cached_off_when_disabled_and_empty_on_error():
    calls = []
    orig = slot_ladder._load
    slot_ladder._load = lambda db: (calls.append(1), {"promoted": {"A|0920"}, "demoted": set(), "cells": {}})[1]
    old = (config.SLOT_LADDER_ENABLED, config.SLOT_LADDER_CACHE_S)
    try:
        slot_ladder.reset()
        config.SLOT_LADDER_ENABLED, config.SLOT_LADDER_CACHE_S = True, 300.0
        assert slot_ladder.get(None)["promoted"] == {"A|0920"} and slot_ladder.get(None)["promoted"] == {"A|0920"} and len(calls) == 1
        slot_ladder.reset(); config.SLOT_LADDER_CACHE_S = 0.0
        slot_ladder.get(None); slot_ladder.get(None)
        assert len(calls) == 3                                                   # no cache = one read per call
        config.SLOT_LADDER_ENABLED = False
        assert slot_ladder.get(None) == {"promoted": set(), "demoted": set(), "cells": {}} and len(calls) == 3
        config.SLOT_LADDER_ENABLED = True
        slot_ladder.reset()
        slot_ladder._load = lambda db: (_ for _ in ()).throw(RuntimeError("db down"))
        assert slot_ladder.get(None)["promoted"] == set()
    finally:
        slot_ladder._load = orig
        config.SLOT_LADDER_ENABLED, config.SLOT_LADDER_CACHE_S = old
        slot_ladder.reset()


def test_wiring_and_defaults():
    assert config.SLOT_LADDER_ENABLED is True and 0.05 <= config.SLOT_LADDER_DEMOTE_FACTOR <= 1.0
    src = open(os.path.join(ROOT, "orders", "entry.py"), encoding="utf-8").read()
    a = src.index("_ladder = slot_ladder.get(db)")
    b = src.index('_inputs["promoted"]', a)
    c = src.index("setup_live.classify_long(", b)
    d = src.index("slot_ladder.demote_factor(", c)
    e = src.index("setup_stamp.build(", d)
    assert a < b < c < d < e                                                     # promoted in before the engine sizes, demotion before the stamp is written
    cat = open(os.path.join(ROOT, "control", "catalog.py"), encoding="utf-8").read()
    assert '"SLOT_LADDER_ENABLED"' in cat


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1; print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
