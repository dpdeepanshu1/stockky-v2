"""Round 650 (E676): the two "sized down instead of refused" INFO lines in orders/entry.py are logged only after the LAST total-penalty
check that can still refuse the entry. 19 Nov 2026 04:48:27 GHCLTEXTIL: "price-guard soft reasons sized down instead of refused", then 1 s
later "skipped (SOFT_PENALTIES_TOO_HIGH ... gain_room 3.0 ...)": the gain-room, VWAP and pre-open-gap penalties are added after the price guard.
attempt_entry needs the whole service to run, so these are source-order checks on entry.py (the same way test_round602 reads it). Plain
functions; `python3 <file>` runs them."""
from __future__ import annotations

import os

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py"), encoding="utf-8").read()
IMPL = SRC[SRC.index("def _attempt_entry_impl("):]


def _at(text, needle, start=0):
    i = text.index(needle, start)
    return i


def test_price_guard_line_is_not_logged_where_it_is_decided():
    i = _at(IMPL, "price-guard soft reasons sized down instead of refused")
    # the text now lives in a stored message tuple, not in a logger call
    assert IMPL[IMPL.rindex("\n", 0, i) - 200:i].count("logger.info(") == 0
    assert "_drift_soft_msg = (" in IMPL[i - 400:i]


def test_price_guard_line_comes_after_the_total_check_that_refuses():
    emit = _at(IMPL, "logger.info(*_drift_soft_msg)")
    tot = _at(IMPL, '"SKIPPED", _tot_reject')
    gain = _at(IMPL, '_penalties["gain_room"]')
    vwap = _at(IMPL, '_penalties["vwap"]')
    pre = _at(IMPL, '_penalties["preopen_gap"]')
    assert max(gain, vwap, pre, tot) < emit


def test_price_guard_message_is_set_only_when_soft_reasons_exist_and_starts_empty():
    assert "_drift_soft_msg = None" in IMPL
    assert IMPL.index("_drift_soft_msg = None") < IMPL.index("_drift_soft_msg = (")


def test_structure_line_comes_after_the_late_total_check():
    s = _at(IMPL, "structure verdict sized down instead of refused")
    late = _at(IMPL, '"SKIPPED", _late_reject')
    assert late < s
    assert "if _struct_pen:" in IMPL[s - 300:s]


def test_no_other_sized_down_line_was_added_or_lost():
    assert SRC.count("sized down instead of refused (%s)") == 2


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("pass", k)
    print(n, "passed")
