"""E273 round 25: an unexplained pre-open gap is a size penalty in the Real Trade long entry. Plain runner: python3 tests/test_e273_preopen_gap_penalty.py (service folder)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from entry_engine import preopen, soft_gates as sg

UP = "PREOPEN_GAP_UP: indicative open +7.0% vs previous close >= +6.0% - not chasing a big gap-up"
DN = "PREOPEN_GAP_DOWN: indicative open -4.0% vs previous close <= -3.0% - not catching a gap-down"


def with_gap(g, fn):
    old_gf, old = preopen.gap_for, {k: getattr(config, k, None) for k in ("SOFT_GATES_MODE", "PREOPEN_GAP_UP_BLOCK_PCT", "PREOPEN_GAP_DOWN_BLOCK_PCT", "PREOPEN_GAP_HARD_MULT")}
    preopen.gap_for = lambda sym, now=None: g
    config.SOFT_GATES_MODE, config.PREOPEN_GAP_UP_BLOCK_PCT, config.PREOPEN_GAP_DOWN_BLOCK_PCT, config.PREOPEN_GAP_HARD_MULT = "penalty", 6.0, 3.0, 2.0
    try:
        return fn()
    finally:
        preopen.gap_for = old_gf
        for k, v in old.items():
            setattr(config, k, v)


def test_no_reason_no_penalty():
    assert sg.preopen_gap("X", None) == (None, 1.0)


def test_gap_up_between_limit_and_double_is_a_penalty():
    why, m = with_gap(7.0, lambda: sg.preopen_gap("X", UP))
    assert why is None and 0.40 < m < 0.85
    m2 = with_gap(10.0, lambda: sg.preopen_gap("X", UP))[1]
    assert m2 < m


def test_gap_up_at_double_the_limit_is_still_refused():
    why, m = with_gap(12.5, lambda: sg.preopen_gap("X", UP))
    assert why == UP and m == 1.0


def test_gap_down_uses_its_own_limit():
    why, m = with_gap(-4.0, lambda: sg.preopen_gap("X", DN))
    assert why is None and m < 0.85
    assert with_gap(-6.5, lambda: sg.preopen_gap("X", DN))[0] == DN


def test_veto_mode_and_missing_gap_keep_the_refusal():
    def f():
        config.SOFT_GATES_MODE = "veto"
        return sg.preopen_gap("X", UP)
    assert with_gap(7.0, f) == (UP, 1.0)
    assert with_gap(None, lambda: sg.preopen_gap("X", UP)) == (UP, 1.0)


def test_wiring():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ent = open(os.path.join(base, "entry_engine", "entry.py")).read()
    assert "soft_gates.preopen_gap(symbol, reason)" in ent and "_preopen_gap_or_waive(cand.symbol, db, _gap_waived, _soft_pen)" in ent
    assert ent.index("return gap_event.against_reason(reason, ev)") < ent.index("soft_gates.preopen_gap(symbol, reason)")   # a gap against an event is still skipped by name


if __name__ == "__main__":
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", n, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
