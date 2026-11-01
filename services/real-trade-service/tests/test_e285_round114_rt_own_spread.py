"""E285 round 114: Real Trade reads the stock's own recorded spread (entry_engine/own_spread, from the files Position Stocks records) and uses it in the bid-ask spread check
(entry._watchlist_depth_reason, at queue time and at the order).
Plain-function runner: python3 tests/test_e285_round114_rt_own_spread.py (also collected by pytest)."""
from __future__ import annotations

import calendar
import json
import os
import shutil
import sys
import tempfile
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import own_spread

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]


def _t0(date: str) -> float:
    y, m, d = (int(x) for x in date.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0)) - 19800 + 9 * 3600 + 15 * 60


def _rows(date: str, pct: float, n: int = 330, start: int = 30):
    return [[_t0(date) + 60 * (start + k), pct, pct * 1.2, 5] for k in range(n)]


def _write(base: str, date: str, symbol: str, rows) -> None:
    """The recorder's own layout: <base>/<date>/<quoted symbol>.json -> {"rows": [[t, median, max, samples], ...]}"""
    p = os.path.join(base, date, quote(symbol, safe="") + ".json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({"v": 1, "symbol": symbol, "date": date, "rows": rows}, f)


def _root(symbol: str, pct: float, days=DAYS, n: int = 330) -> str:
    """A recorder root (the parent of its `spread` folder), as Real Trade finds it through ENTRY_OWN_NORMS_BARS_DIR."""
    root = tempfile.mkdtemp(prefix="rt_own_spread_")
    for d in days:
        _write(os.path.join(root, "spread"), d, symbol, _rows(d, pct, n=n))
    return root


def _on(root: str):
    config.ENTRY_OWN_SPREAD_ENABLED = True
    config.ADAPTIVE_ENTRY_PARAMS = True
    config.ENTRY_OWN_NORMS_BARS_DIR = root
    own_spread.reset()


def _off():
    config.ENTRY_OWN_SPREAD_ENABLED = False
    config.ENTRY_OWN_NORMS_BARS_DIR = ""
    own_spread.reset()


def _wait(symbol: str, secs: float = 3.0):
    end = time.time() + secs
    while time.time() < end:
        nm = own_spread._norms(symbol)
        if nm is not None:
            return nm
        time.sleep(0.02)
    return None


def test_the_root_is_the_same_folder_position_stocks_writes_into():
    config.ENTRY_OWN_NORMS_BARS_DIR = "/x/bars"
    try:
        assert own_spread.spread_root() == os.path.join("/x/bars", "spread")
    finally:
        config.ENTRY_OWN_NORMS_BARS_DIR = ""
    assert own_spread.spread_root() == "/data/bars/spread"          # BAR_RECORDS_DIR /data/bars/rt -> /data/bars


def test_day_p90_uses_only_the_middle_of_the_session_and_needs_enough_rows():
    d = DAYS[0]
    rows = _rows(d, 0.2) + [[_t0(d) + 60 * 2, 3.0, 3.0, 1]]                 # a wide opening minute must not count
    assert abs(own_spread.day_p90(rows, d) - 0.2) < 1e-9
    assert own_spread.day_p90(_rows(d, 0.2, n=40), d) is None               # 40 readings < 60: a thin day says nothing
    assert own_spread.day_p90([], d) is None


def test_rows_are_cleaned_like_the_recorder_does():
    bad = [[_t0(DAYS[0]), 0.0, 0.0, 1], [_t0(DAYS[0]) + 60, 30.0, 30.0, 1], [_t0(DAYS[0]) + 120, 0.3, 0.2, 1], ["x"], [_t0(DAYS[0]) + 180, 0.2, 0.3, 0],
           [_t0(DAYS[0]) + 240, 0.2, 0.3, 4]]
    assert own_spread._clean_rows(bad) == [[_t0(DAYS[0]) + 240, 0.2, 0.3, 4]]


def test_build_reads_each_usable_day_and_only_before_today():
    root = _root("AAA", 0.2)
    try:
        base = os.path.join(root, "spread")
        os.makedirs(os.path.join(base, "not-a-date"), exist_ok=True)
        nm = own_spread.build("AAA", today="2026-11-02", root=base)
        assert nm and nm["days"] == 8 and abs(nm["p90_pct"] - 0.2) < 1e-9, nm
        assert own_spread.build("AAA", today="2026-10-20", root=base)["days"] == 1
        assert own_spread.build("MISSING", today="2026-11-02", root=base) is None
        assert own_spread.build("AAA", today="2026-11-02", root=os.path.join(root, "nope")) is None      # no recorder folder at all: unknown, no raise
    finally:
        shutil.rmtree(root)


def test_a_symbol_with_a_dot_or_dash_is_read_from_its_quoted_file_name():
    root = tempfile.mkdtemp(prefix="rt_own_spread_q_")
    try:
        for d in DAYS:
            _write(os.path.join(root, "spread"), d, "M&M", _rows(d, 0.1))
        _on(root)
        assert _wait("M&M.NS")["days"] == 8
    finally:
        _off(); shutil.rmtree(root)


def test_constant_with_too_few_days():
    root = _root("AAA", 0.2, days=DAYS[:4])
    try:
        _on(root)
        assert _wait("AAA")["days"] == 4
        assert own_spread.ceiling_pct("AAA", 0.5)["source"] == "constant"
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
    finally:
        config.ADAPTIVE_ENTRY_PARAMS = True
        _off(); shutil.rmtree(root)


def test_missing_history_is_cached_as_unknown():
    root = tempfile.mkdtemp(prefix="rt_own_spread_empty_")
    try:
        _on(root)
        own_spread._norms("NOPE")
        end = time.time() + 3
        while time.time() < end and own_spread._running:
            time.sleep(0.02)
        assert "NOPE" in own_spread._cache and own_spread._cache["NOPE"][0] is None
    finally:
        _off(); shutil.rmtree(root)


def test_warm_queues_symbols_and_never_raises():
    root = _root("AAA", 0.08)
    try:
        _on(root)
        assert own_spread.warm(["AAA", "BBB"]) == 2 and own_spread.warm(None) == 0
        assert _wait("AAA") is not None
    finally:
        _off(); shutil.rmtree(root)


class _Tick:
    def __init__(self, spread=None, book=None, symbol=None):
        self.spread_pct, self.book_value_5 = spread, book
        if symbol is not None:
            self.symbol = symbol


def _entry():
    from entry_engine import entry
    return entry


def test_the_spread_check_uses_the_stocks_own_ceiling():
    entry = _entry()
    root = _root("LIQ", 0.08)
    try:
        _on(root)
        _wait("LIQ")
        own = entry._own_spread_ceiling("LIQ", 0.5)
        assert 0.25 <= own < 0.5
        mid = (own + 0.5) / 2.0                                                          # between its own ceiling and the fixed 0.5 %
        assert entry._watchlist_depth_reason(_Tick(spread=mid, symbol="LIQ.NS")) is not None      # a liquid name's own ceiling refuses it
        assert entry._watchlist_depth_reason(_Tick(spread=mid)) is None                  # no symbol on the tick: the fixed 0.5 %
        assert entry._watchlist_depth_reason(_Tick(spread=mid, symbol="UNSEEN")) is None  # no history: the fixed 0.5 %
        assert entry._watchlist_depth_reason(_Tick(spread=own * 0.9, symbol="LIQ")) is None
        assert entry._watchlist_depth_reason(_Tick(spread=0.9, symbol="UNSEEN")) is not None   # the fixed ceiling still refuses a wide spread
        config.ADAPTIVE_ENTRY_PARAMS = False
        assert entry._watchlist_depth_reason(_Tick(spread=mid, symbol="LIQ")) is None   # the master switch restores the fixed ceiling
    finally:
        config.ADAPTIVE_ENTRY_PARAMS = True
        _off(); shutil.rmtree(root)


def test_a_thin_stock_may_be_wider_than_the_fixed_ceiling():
    entry = _entry()
    root = _root("THINNY", 0.45)
    try:
        _on(root)
        _wait("THINNY")
        own = entry._own_spread_ceiling("THINNY", 0.5)
        assert own > 0.5 and own <= 0.75, own
        assert entry._watchlist_depth_reason(_Tick(spread=0.55, symbol="THINNY")) is None     # refused by the fixed 0.5 %, passed by its own
        assert entry._watchlist_depth_reason(_Tick(spread=0.8, symbol="THINNY")) is not None  # never past the +50 % bound
    finally:
        _off(); shutil.rmtree(root)


def test_the_gate_off_stays_off_and_the_book_test_is_untouched():
    entry = _entry()
    root = _root("LIQ", 0.08)
    try:
        _on(root)
        _wait("LIQ")
        assert entry._own_spread_ceiling("LIQ", 0.0) == 0.0                                # 0 = gate off stays off
        os.environ["WATCHLIST_MIN_BOOK_VALUE"] = "1000000"
        try:
            why = entry._watchlist_depth_reason(_Tick(spread=0.1, book=500000, symbol="LIQ"))
            assert why and "best-5 book" in why, why
        finally:
            os.environ.pop("WATCHLIST_MIN_BOOK_VALUE", None)
    finally:
        _off(); shutil.rmtree(root)


def test_wiring_in_the_entry_path_and_the_warm_up():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "entry_engine", "entry.py"), encoding="utf-8").read()
    i = src.index("def _watchlist_depth_reason")
    assert "_own_spread_ceiling(getattr(tick," in src[i:i + 900]
    assert "own_spread.warm(" in open(os.path.join(here, "entry_engine", "preopen.py"), encoding="utf-8").read()


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
