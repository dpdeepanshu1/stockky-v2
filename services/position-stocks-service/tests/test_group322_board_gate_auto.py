"""group322: BOARD_GATE_MODE=auto enforces only while the shadow report says the chase guard is promoted."""
from __future__ import annotations

import pytest

import config
from orders import board_gate
from screening import board_log


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    board_log.reset()
    board_gate.reset_auto()
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "auto", raising=False)
    monkeypatch.setattr(config, "BOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BOARD_CHASE_ATR", 1.5, raising=False)
    monkeypatch.setattr(config, "BOARD_AUTO_REFRESH_S", 3600, raising=False)
    monkeypatch.setattr(board_log, "_ensure_thread", lambda: None)
    monkeypatch.setattr(board_gate, "evaluate",
                        lambda *a: {"chase_atr": 2.4, "zone_status": "fresh", "rr": 2.0, "extended": True})
    yield
    board_gate.reset_auto()
    board_log.reset()


def test_default_config_is_auto():
    import importlib, os
    os.environ.pop("BOARD_GATE_MODE", None)
    assert config.BOARD_AUTO_REFRESH_S >= 60 and 1 <= config.BOARD_AUTO_DAYS <= 120


def test_stays_shadow_until_promoted(monkeypatch):
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: "needs more data (3/30 obs, 2/10 sessions)")
    assert board_gate.effective_mode() == "shadow"
    assert board_gate.check("A", 100.0) is None            # shadow: logs, never refuses


def test_enforces_once_promoted_and_refuses_extended(monkeypatch):
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: "promote")
    assert board_gate.effective_mode() == "enforce"
    r = board_gate.check("A", 100.0)
    assert r and r.startswith("BOARD_CHASE:")


def test_verdict_cached_between_entries(monkeypatch):
    calls = []
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: calls.append(1) or "promote")
    board_gate.effective_mode(); board_gate.effective_mode(); board_gate.check("A", 100.0)
    assert len(calls) == 1


def test_drops_back_to_shadow_when_verdict_stops_holding(monkeypatch):
    v = {"x": "promote"}
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: v["x"])
    assert board_gate.effective_mode() == "enforce"
    v["x"] = "do not promote"
    board_gate._auto["at"] -= 7200                          # cache expired
    assert board_gate.effective_mode() == "shadow"


def test_failure_keeps_last_answer_and_fails_open(monkeypatch):
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: "promote")
    assert board_gate.effective_mode() == "enforce"
    board_gate._auto["at"] -= 7200
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert board_gate.effective_mode() == "enforce"         # last good answer kept
    board_gate.reset_auto()
    assert board_gate.effective_mode() == "shadow"          # never had one -> shadow
    assert board_gate.check("A", 100.0) is None


def test_explicit_modes_untouched(monkeypatch):
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "enforce", raising=False)
    monkeypatch.setattr(board_gate, "_auto_verdict", lambda: (_ for _ in ()).throw(AssertionError("not used")))
    assert board_gate.effective_mode() == "enforce"
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "off", raising=False)
    assert board_gate.check("A", 100.0) is None


def test_auto_verdict_runs_on_an_empty_table(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    import db as dbmod
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: sessionmaker(bind=eng))
    assert board_gate._auto_verdict().startswith("needs more data")
