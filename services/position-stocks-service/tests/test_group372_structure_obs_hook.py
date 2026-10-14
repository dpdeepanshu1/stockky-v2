"""group 372: the position-stocks structure observation is hooked in - gate capture, the entry record call, the boot loop and GET /structure/edge.
Source-text / AST checks for the wiring (main.py and entry.py are heavy to boot), plus behaviour tests for the capture and a real-SQLite round trip."""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)

import config
from orders import structure_gate as sg
from orders import structure_observation as so


def _src(rel):
    return open(os.path.join(_SERVICE, rel), encoding="utf-8").read()


MAIN_SRC, ENTRY_SRC = _src("main.py"), _src("orders/entry.py")


def _routes():
    out = {}
    for n in ast.walk(ast.parse(MAIN_SRC)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post") and d.args:
                    out.setdefault((d.func.attr, d.args[0].value), n.name)
    return out


def test_edge_route_registered_and_read_only():
    assert _routes().get(("get", "/structure/edge")) == "structure_edge"
    fn = next(n for n in ast.walk(ast.parse(MAIN_SRC)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "structure_edge")
    seg = ast.get_source_segment(MAIN_SRC, fn)
    assert "asyncio.to_thread(_so.report" in seg and "except Exception" in seg


def test_boot_loop_is_switchable_isolated_and_cancelled_on_shutdown():
    assert 'getattr(config, "STRUCTURE_OBS_ENABLED", True)' in MAIN_SRC
    assert "_struct_obs.run_loop(_db.get_session_factory())" in MAIN_SRC
    assert "_struct_obs_task.cancel()" in MAIN_SRC
    i = MAIN_SRC.index("_struct_obs_task = asyncio.create_task")
    assert "try:" in MAIN_SRC[i - 200:i] and "except Exception" in MAIN_SRC[i:i + 400]


def test_entry_records_after_the_gate_and_before_any_return():
    assert "structure_observation" in ENTRY_SRC.split("from orders.adaptive", 1)[0]
    i = ENTRY_SRC.index("structure_gate.check(candidate.symbol, candidate.current_ltp, capture=_struct_cap)")
    j = ENTRY_SRC.index("structure_observation.record(db, candidate.symbol", i)
    k = ENTRY_SRC.index("if _struct_reject:", i)
    assert i < j < k   # recorded for passed AND rejected verdicts, before the reject return


def test_config_defaults():
    assert config.STRUCTURE_OBS_ENABLED is True
    assert config.STRUCTURE_OBS_EVAL_INTERVAL_S >= 60.0 and config.STRUCTURE_OBS_BATCH >= 1
    assert config.STRUCTURE_OBS_HISTORY_TIMEOUT_S >= 1.0 and config.STRUCTURE_OBS_GIVE_UP_DAYS >= 5.0 and config.STRUCTURE_EDGE_MIN_N >= 1


def test_env_vars_documented():
    env = open(os.path.join(_SERVICE, "..", "..", ".env.example"), encoding="utf-8").read()
    for v in ("STRUCTURE_OBS_ENABLED", "STRUCTURE_OBS_EVAL_INTERVAL_S", "STRUCTURE_OBS_BATCH", "STRUCTURE_OBS_HISTORY_TIMEOUT_S",
              "STRUCTURE_OBS_GIVE_UP_DAYS", "STRUCTURE_EDGE_MIN_N"):
        assert v in env, v


def _summary(trend="bullish"):
    return {"ms_quality": "ok", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": trend,
            "ms_stop": 95.0, "ms_target": 110.0}


def test_capture_is_filled_for_a_pass_and_a_reject_and_stays_empty_when_off():
    sg.reset()
    for trend, rejected in (("bullish", False), ("bearish", True)):
        cap: dict = {}
        with mock.patch.object(sg, "fetch_summary", lambda s, t=trend: _summary(t)), mock.patch.object(config, "STRUCTURE_GATE_MODE", "enforce"):
            reason, _snap = sg.check("GOODCO", 100.0, capture=cap)
        assert bool(reason) is rejected and cap["enforced"] is rejected and cap["gate_mode"] == "enforce" and cap["summary"]["ms_stop"] == 95.0
    cap = {}
    with mock.patch.object(sg, "fetch_summary", lambda s: _summary()), mock.patch.object(config, "STRUCTURE_GATE_MODE", "off"):
        assert sg.check("GOODCO", 100.0, capture=cap) == (None, None) and cap == {}


def test_capture_survives_a_failing_gate_and_a_non_dict():
    sg.reset()
    cap: dict = {}
    with mock.patch.object(sg, "fetch_summary", lambda s: (_ for _ in ()).throw(RuntimeError("down"))), mock.patch.object(config, "STRUCTURE_GATE_MODE", "enforce"):
        assert sg.check("X", 100.0, capture=cap) == (None, None) and cap == {}
        assert sg.check("X", 100.0, capture="nope") == (None, None)


def test_record_called_the_way_entry_calls_it_on_a_real_sqlite_table():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    eng = create_engine("sqlite://")
    models.ScalpStructureObservation.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    sg.reset()
    so.reset()
    cap: dict = {}
    with mock.patch.object(sg, "fetch_summary", lambda s: _summary("bearish")), mock.patch.object(config, "STRUCTURE_GATE_MODE", "enforce"):
        sg.check("GOODCO", 100.0, capture=cap)
    args = ("GOODCO", 100.0, cap.get("verdict") or {}, cap.get("summary"), cap.get("gate_mode") or "", bool(cap.get("enforced")))
    assert so.record(db, *args) is True
    db.commit()
    row = db.query(models.ScalpStructureObservation).one()
    assert row.verdict == "rejected_enforced" and row.source == "position_stocks" and row.stop == 95.0 and row.is_complete is False
    so.reset()                                   # a restart: the DB, not the in-process memory, keeps one row per symbol / day
    assert so.record(db, *args) is False
    assert db.query(models.ScalpStructureObservation).count() == 1
    rep = so.report(db, days=7)
    assert rep["stats"]["total"] == 1 and rep["recent"][0]["symbol"] == "GOODCO"
