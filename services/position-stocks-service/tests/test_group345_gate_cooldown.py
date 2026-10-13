"""group345: a symbol rejected by a price-only entry gate is left out of the candidate list for GATE_REJECT_COOLDOWN_S."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import main


def test_gate_reject_cooldown(monkeypatch):
    main._gate_rejected.clear()
    monkeypatch.setattr(config, "GATE_REJECT_COOLDOWN_S", 60.0, raising=False)
    main._note_gate_reject("SAKSOFT", "PRICE_GUARD:up 10.2% on the day")
    main._note_gate_reject("EMIL", "VWAP_BELOW:...")          # not a price-only gate: no cooldown
    cands = [SimpleNamespace(symbol=s) for s in ("SAKSOFT", "EMIL", "TRENT")]
    kept, skipped = main._gate_cooldown_filter(cands)
    assert [c.symbol for c in kept] == ["EMIL", "TRENT"] and skipped == ["SAKSOFT"]
    main._gate_rejected["SAKSOFT"] = 0.0                          # cooldown over
    kept, skipped = main._gate_cooldown_filter(cands)
    assert len(kept) == 3 and skipped == []
    monkeypatch.setattr(config, "GATE_REJECT_COOLDOWN_S", 0.0, raising=False)
    main._note_gate_reject("GRMOVER", "RANGE_GATE:top of range")
    assert "GRMOVER" not in main._gate_rejected
    main._gate_rejected.clear()
