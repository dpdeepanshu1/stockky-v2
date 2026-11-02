"""E285 round 137: the 14:00 scalp cut-off is checked before the Time Machine gate in attempt_entry (it used to be after it, so every cycle from 14:00 ran the replay and
was refused anyway). Same refusal and outcome; no replay, no later gate runs."""
from pathlib import Path

import config
from orders import entry, tm_gate, calendar_gate
from tests.test_short_step5_shadow_entry import db, cand  # noqa: F401  (db is the shared fixture)

SRC = (Path(__file__).resolve().parents[1] / "orders" / "entry.py").read_text(encoding="utf-8")


def _arm(db):
    g = entry._get_gate_state(db)
    g.is_armed = True
    db.commit()


def _logged(db):
    from models import ScalpCandidateLog
    return [(r.decision, r.reason) for r in db.query(ScalpCandidateLog).all()]


def test_after_the_cutoff_the_time_machine_is_never_asked(db, monkeypatch):
    _arm(db)
    monkeypatch.setattr(calendar_gate, "scalp_cutoff_reason", lambda now=None: "SCALP_ENTRY_CUTOFF:no new scalp entry after 14:00 IST")
    monkeypatch.setattr(tm_gate, "entry_check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the replay must not run after the cut-off")))
    assert entry.attempt_entry(db, cand("RKFORGE")) is None
    rows = _logged(db)
    assert rows and rows[-1][0] == "SKIPPED" and rows[-1][1].startswith("SCALP_ENTRY_CUTOFF"), rows


def test_before_the_cutoff_the_early_check_lets_the_candidate_through_to_the_other_gates(db, monkeypatch):
    _arm(db)
    monkeypatch.setattr(calendar_gate, "scalp_cutoff_reason", lambda now=None: None)
    monkeypatch.setattr(entry, "_count_open_positions", lambda d: 99)          # a later, ordinary refusal: proves the walk went past the early check
    assert entry.attempt_entry(db, cand("X")) is None
    assert _logged(db)[-1][1].startswith("MAX_POSITIONS:99")


def test_order_in_the_source_cutoff_then_time_machine_and_the_late_check_stays():
    assert SRC.index("_cut_early = calendar_gate.scalp_cutoff_reason()") < SRC.index("tm_gate.entry_check(candidate.symbol")
    assert SRC.index("_cut_reject = calendar_gate.scalp_cutoff_reason()") > SRC.index("tm_gate.entry_check(candidate.symbol")      # the original stays (re-armed path)
