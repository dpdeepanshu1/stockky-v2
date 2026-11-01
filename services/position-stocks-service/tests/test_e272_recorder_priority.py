"""E272: the bar recorder writes noted candidates beyond the turnover cut. Plain runner: python3 tests/test_e272_recorder_priority.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from feed import bar_recorder as br

NOW = 1_793_000_000.0           # a Tuesday during the session
DN = int((NOW + br.IST_OFFSET_S) // 86400)


def _run(root, syms, noted=None, max_syms=2):
    br.reset()
    config.BAR_RECORD_DIR, config.BAR_RECORD_MAX_SYMBOLS = root, max_syms
    config.BAR_RECORD_ENABLED = True            # E284: conftest switches the recorder off for every older test; this one is about the recorder (conftest restores it)
    if noted is not None:
        br.note_symbols(noted, NOW)
    bars = {s: [[NOW - 60 * (30 - i), 10.0, 11.0, 9.0, 10.0, 5] for i in range(30)] for s in syms}
    turn = {s: float(100 - i) for i, s in enumerate(syms)}
    # bars_fn / symbols_fn / day_fn are test hooks; turnover is read from the store, so patch it through symbols order instead
    return br.run_once(NOW, True, bars_fn=lambda s: bars[s], symbols_fn=lambda: list(syms), day_fn=lambda: DN)


def _files(root):
    out = []
    for d in os.listdir(root):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            out += [n for n in os.listdir(p) if n.endswith(".json")]
    return sorted(out)


def test_without_noting_only_the_cap_is_written():
    with tempfile.TemporaryDirectory() as d:
        _run(d, ["AAA", "BBB", "CCC", "DDD"])
        assert len(_files(d)) == 2


def test_noted_candidates_outside_the_cap_are_written_too():
    with tempfile.TemporaryDirectory() as d:
        _run(d, ["AAA", "BBB", "CCC", "DDD"], noted=["DDD", "CCC.NS"])
        assert len(_files(d)) == 4


def test_noted_symbol_the_store_never_saw_is_counted_not_written():
    with tempfile.TemporaryDirectory() as d:
        _run(d, ["AAA", "BBB"], noted=["ZZZ"])
        assert br.stats()["priority_missing"] == 1 and len(_files(d)) == 2


def test_switch_off_and_cap_and_day_reset():
    with tempfile.TemporaryDirectory() as d:
        config.BAR_RECORD_PRIORITY_ENABLED = False
        try:
            _run(d, ["AAA", "BBB", "CCC"], noted=["CCC"])
            assert len(_files(d)) == 2 and br.note_symbols(["X"]) == 0
        finally:
            config.BAR_RECORD_PRIORITY_ENABLED = True
    br.reset()
    config.BAR_RECORD_PRIORITY_MAX = 2
    try:
        assert br.note_symbols(["A", "B", "C"], NOW) == 2
        assert br.note_symbols(["D"], NOW + 86400) == 1            # a new IST day starts clean
    finally:
        config.BAR_RECORD_PRIORITY_MAX = 400


def test_wiring_in_entry_short_entry_and_preopen():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("orders/entry.py", "orders/short_entry.py", "screening/preopen.py"):
        assert "note_symbols" in open(os.path.join(root, rel), encoding="utf-8").read(), rel


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
