"""E249: per-symbol sizing / level rejects fall through to the next shortlisted pick (and cool down), and the day-mover scan never lists a move entry refuses."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from orders import entry  # noqa: E402
from screening import engine  # noqa: E402


@pytest.mark.parametrize("reason", [
    "MIN_QTY:qty=2<3:ltp=1003.00", "RR_FLOOR: reward:risk 1.50:1 (target 1.20% / stop 0.80%) is below the 2:1 floor",
    "STOP_ATR_FLOOR:STOP_TOO_TIGHT:stop 0.40% < 0.30 x", "MIN_PRICE:ltp=5.00<10.00",
])
def test_new_symbol_specific_reasons_fall_through(reason):
    assert entry.is_symbol_specific_skip(reason) is True


@pytest.mark.parametrize("reason", ["MAX_POSITIONS:5", "SERVICE_NOT_ARMED", "DAILY_LOSS_KILL_SWITCH", "ORDER_BUDGET_EXHAUSTED:40", "QUALITY_GATE:x"])
def test_global_reasons_still_end_the_cycle(reason):
    assert entry.is_symbol_specific_skip(reason) is False


def test_cooldown_prefixes_cover_the_new_reasons():
    import main
    for r in ("MIN_QTY:qty=2<3", "RR_FLOOR:x", "STOP_ATR_FLOOR:x", "MIN_PRICE:x"):
        main._gate_rejected.clear()
        main._note_gate_reject("ABC", r)
        assert "ABC" in main._gate_rejected, r
    main._gate_rejected.clear()


def test_day_mover_cap_follows_entry_guards(monkeypatch):
    monkeypatch.setattr(config, "DAY_MOVER_MAX_PCT", 20.0)
    monkeypatch.setattr(config, "MAX_DAY_GAIN_PCT", 7.0)
    monkeypatch.setattr(config, "SHORT_MAX_DAY_FALL_PCT", 5.0)
    assert engine._day_mover_max("LONG") == 7.0
    assert engine._day_mover_max("SHORT") == 5.0
    monkeypatch.setattr(config, "MAX_DAY_GAIN_PCT", 0.0)       # guard off -> only DAY_MOVER_MAX_PCT
    assert engine._day_mover_max("LONG") == 20.0
    monkeypatch.setattr(config, "MAX_DAY_GAIN_PCT", 30.0)      # guard looser than the scan cap -> scan cap
    assert engine._day_mover_max("LONG") == 20.0


def test_a_six_percent_faller_is_not_listed_as_a_day_mover_short():
    from tests.test_e232_scan_hits_day_mover import _feed, _flat   # reuse the feed stub
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        _feed(mp, {"SLIDE": _flat(94.0)}, {"SLIDE": (100.0, 100.0, 93.0, 100.0)})
        assert engine.scan_shorts() == []
    finally:
        mp.undo()
