"""E285 round 112: the stock's own spread ceiling (orders/own_spread) and its use in the long and short depth gates.
Plain-function runner: python3 tests/test_e285_round112_own_spread.py (also collected by pytest)."""
from __future__ import annotations

import calendar
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import spread_recorder as sr
from orders import depth_gate, own_spread, short_gates

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]


def _t0(date: str) -> float:
    y, m, d = (int(x) for x in date.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0)) - 19800 + 9 * 3600 + 15 * 60


def _rows(date: str, pct: float, n: int = 330, start: int = 30):
    return [[_t0(date) + 60 * (start + k), pct, pct * 1.2, 5] for k in range(n)]


def _root(symbol: str, pct: float, days=DAYS, n: int = 330) -> str:
    root = tempfile.mkdtemp(prefix="own_spread_")
    for d in days:
        sr.write_symbol(root, d, symbol, _rows(d, pct, n=n))
    return root


def _on(root: str):
    config.ENTRY_OWN_SPREAD_ENABLED = True
    config.ADAPTIVE_ENTRY_PARAMS = True
    config.BAR_RECORD_DIR = root
    own_spread.reset()


def _off():
    config.ENTRY_OWN_SPREAD_ENABLED = False
    own_spread.reset()


def _wait(symbol: str, secs: float = 3.0):
    end = time.time() + secs
    while time.time() < end:
        nm = own_spread._norms(symbol)
        if nm is not None:
            return nm
        time.sleep(0.02)
    return None


def test_day_p90_uses_only_the_middle_of_the_session_and_needs_enough_rows():
    d = DAYS[0]
    rows = _rows(d, 0.2) + [[_t0(d) + 60 * 2, 3.0, 3.0, 1]]                 # a wide opening minute must not count
    assert abs(own_spread.day_p90(rows, d) - 0.2) < 1e-9
    assert own_spread.day_p90(_rows(d, 0.2, n=40), d) is None               # 40 readings < 60: a thin day says nothing
    assert own_spread.day_p90([], d) is None


def test_build_reads_each_usable_day_and_only_before_today():
    root = _root("AAA", 0.2)
    try:
        nm = own_spread.build("AAA", today="2026-11-02", root=root)
        assert nm and nm["days"] == 8 and abs(nm["p90_pct"] - 0.2) < 1e-9, nm
        assert own_spread.build("AAA", today="2026-10-20", root=root)["days"] == 1
        assert own_spread.build("MISSING", today="2026-11-02", root=root) is None
    finally:
        shutil.rmtree(root)


def test_constant_with_too_few_days():
    root = _root("AAA", 0.2, days=DAYS[:4])
    try:
        _on(root)
        assert _wait("AAA")["days"] == 4
        assert own_spread.ceiling_pct("AAA", 0.5)["source"] == "constant"
        assert depth_gate.own_max_spread_pct("AAA", 0.5) == 0.5
    finally:
        _off(); shutil.rmtree(root)


def test_liquid_stock_gets_a_tighter_ceiling_within_the_band():
    root = _root("LIQ", 0.08)
    try:
        _on(root)
        _wait("LIQ")
        out = own_spread.ceiling_pct("LIQ", 0.5)
        w = 8 / 18.0
        assert out["source"] == "learned" and abs(out["value"] - (w * 0.12 + (1 - w) * 0.5)) < 1e-6, out       # 0.08 x 1.5 = 0.12, shrunk toward 0.5
        assert 0.25 <= out["value"] < 0.5
        assert abs(depth_gate.own_max_spread_pct("LIQ", 0.5) - out["value"]) < 1e-9
    finally:
        _off(); shutil.rmtree(root)


def test_hard_band_both_ways():
    many = ["2026-09-%02d" % d for d in range(1, 21)]
    r1, r2 = _root("TIGHT", 0.01, days=many), _root("THIN", 3.0, days=many)
    try:
        _on(r1); _wait("TIGHT")
        out = own_spread.ceiling_pct("TIGHT", 0.5)
        assert out["source"] == "floor" and abs(out["value"] - 0.25) < 1e-9, out
        _off(); _on(r2); _wait("THIN")
        out = own_spread.ceiling_pct("THIN", 0.5)
        assert out["source"] == "ceiling" and abs(out["value"] - 0.75) < 1e-9, out
    finally:
        _off(); shutil.rmtree(r1); shutil.rmtree(r2)


def test_switches_and_unknowns_give_no_opinion():
    root = _root("AAA", 0.08)
    try:
        _on(root)
        config.ENTRY_OWN_SPREAD_ENABLED = False
        assert own_spread.ceiling_pct("AAA", 0.5) is None
        config.ENTRY_OWN_SPREAD_ENABLED, config.ADAPTIVE_ENTRY_PARAMS = True, False       # the master switch wins
        assert own_spread.ceiling_pct("AAA", 0.5) is None and not own_spread.enabled()
        config.ADAPTIVE_ENTRY_PARAMS = True
        assert own_spread.ceiling_pct("^NSEI", 0.5) is None and own_spread.ceiling_pct("AAA", 0.0) is None
        own_spread.reset()
        assert own_spread.ceiling_pct("AAA", 0.5) is None                                 # first ask: unknown, never waits
        assert depth_gate.own_max_spread_pct("AAA", 0.0) == 0.0                            # 0 = gate off stays off
    finally:
        config.ADAPTIVE_ENTRY_PARAMS = True
        _off(); shutil.rmtree(root)


def test_missing_history_is_cached_as_unknown():
    root = tempfile.mkdtemp(prefix="own_spread_empty_")
    try:
        _on(root)
        own_spread._norms("NOPE")
        end = time.time() + 3
        while time.time() < end and own_spread._running:
            time.sleep(0.02)
        assert "NOPE" in own_spread._cache and own_spread._cache["NOPE"][0] is None
    finally:
        _off(); shutil.rmtree(root)


def test_short_depth_gate_uses_the_own_ceiling():
    root = _root("LIQ", 0.08)
    saved = config.ENTRY_DEPTH_MAX_SPREAD_PCT
    config.ENTRY_DEPTH_MAX_SPREAD_PCT = 0.5
    try:
        _on(root)
        _wait("LIQ")
        own = depth_gate.own_max_spread_pct("LIQ", 0.5)
        q = {"spread_pct": (own + 0.5) / 2.0, "book_value_5": 1e7}                         # between its own ceiling and the fixed 0.5 %
        assert short_gates.depth_gate_short({"quote": q, "symbol": "LIQ"}, None) is not None       # a liquid name's own ceiling refuses it
        assert short_gates.depth_gate_short({"quote": q}, None) is None                    # no symbol in the facts: the fixed ceiling
        assert short_gates.depth_gate_short({"quote": q, "symbol": "UNSEEN"}, None) is None
    finally:
        config.ENTRY_DEPTH_MAX_SPREAD_PCT = saved
        _off(); shutil.rmtree(root)


def test_long_depth_gate_refuses_by_the_own_ceiling():
    root = _root("LIQ", 0.08)
    saved = (config.ENTRY_DEPTH_GATE, config.ENTRY_DEPTH_MAX_SPREAD_PCT, config.ENTRY_MIN_BOOK_VALUE)
    config.ENTRY_DEPTH_GATE, config.ENTRY_DEPTH_MAX_SPREAD_PCT, config.ENTRY_MIN_BOOK_VALUE = True, 0.5, 0.0
    orig = depth_gate._fresh_quote
    try:
        _on(root)
        _wait("LIQ")
        own = depth_gate.own_max_spread_pct("LIQ", 0.5)
        depth_gate._fresh_quote = lambda s: {"spread_pct": (own + 0.5) / 2.0, "book_value_5": 1e7, "source": "t"}
        why = depth_gate.reject_reason("LIQ")
        assert why and why.startswith("DEPTH_SPREAD"), why
        assert depth_gate.reject_reason("UNSEEN") is None                                  # no history: 0.5 % fixed ceiling, the spread is under it
        config.ADAPTIVE_ENTRY_PARAMS = False
        assert depth_gate.reject_reason("LIQ") is None
    finally:
        depth_gate._fresh_quote = orig
        config.ADAPTIVE_ENTRY_PARAMS = True
        config.ENTRY_DEPTH_GATE, config.ENTRY_DEPTH_MAX_SPREAD_PCT, config.ENTRY_MIN_BOOK_VALUE = saved
        _off(); shutil.rmtree(root)


def test_warm_queues_symbols_and_never_raises():
    root = _root("AAA", 0.08)
    try:
        _on(root)
        assert own_spread.warm(["AAA", "BBB"]) == 2 and own_spread.warm(None) == 0
        assert _wait("AAA") is not None
    finally:
        _off(); shutil.rmtree(root)


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e)); traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
