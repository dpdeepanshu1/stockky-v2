"""group322 -> group370: the board chase guard has no "auto" / "shadow" mode any more. enforce (default, always live) | off (kill switch)."""
from __future__ import annotations

import config
from orders import board_gate


def test_default_is_enforce_and_off_is_the_only_switch(monkeypatch):
    for v in ("enforce", "auto", "shadow", "bogus", ""):
        monkeypatch.setattr(config, "BOARD_GATE_MODE", v, raising=False)
        assert board_gate.effective_mode() == "enforce", v
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "off", raising=False)
    assert board_gate.effective_mode() == "off"


def test_auto_machinery_is_gone():
    assert not hasattr(board_gate, "reset_auto") and not hasattr(board_gate, "_auto_verdict")
    assert not hasattr(config, "BOARD_AUTO_REFRESH_S") and not hasattr(config, "BOARD_AUTO_DAYS")
