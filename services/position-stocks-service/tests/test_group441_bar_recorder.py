"""group441 (WS-15): feed/bar_recorder.py - disk layout, atomic writes, restart merge, cadence, final flush, retention."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import bar_recorder as br

IST = timezone(timedelta(hours=5, minutes=30))
_ORIG = {}
_TMP = []


def setup_function(_):
    for k in ("BAR_RECORD_ENABLED", "BAR_RECORD_DIR", "BAR_RECORD_EVERY_S", "BAR_RECORD_MAX_SYMBOLS", "BAR_RECORD_MIN_BARS", "BAR_RECORD_KEEP_DAYS"):
        _ORIG.setdefault(k, getattr(config, k, None))
    br.reset()
    d = tempfile.mkdtemp(prefix="barrec_")
    _TMP.append(d)
    config.BAR_RECORD_ENABLED = True
    config.BAR_RECORD_DIR = d
    config.BAR_RECORD_EVERY_S = 300
    config.BAR_RECORD_MAX_SYMBOLS = 250
    config.BAR_RECORD_MIN_BARS = 20
    config.BAR_RECORD_KEEP_DAYS = 45


def teardown_function(_):
    for k, v in _ORIG.items():
        setattr(config, k, v)
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    _TMP.clear()
    br.reset()


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()


def _bars(day_open_ts, n, start=0, px=100.0):
    return [(day_open_ts + (start + k) * 60.0, px + k * 0.01, px + k * 0.01 + 0.2, px + k * 0.01 - 0.2, px + k * 0.01 + 0.05, 100.0 + k) for k in range(n)]


OPEN = _ts(2026, 10, 19, 9, 15)
DAYNUM = int((OPEN + 19800) // 86400)


def _day_fn():
    return DAYNUM


def test_file_names_round_trip_for_awkward_symbols():
    for sym in ("M&M", "BAJAJ-AUTO", "L&TFH", "NIFTY 50", "A/B"):
        path = br.file_for("/r", "2026-10-19", sym)
        assert os.path.basename(path).endswith(".json") and "/" not in os.path.basename(path)
        assert br.symbol_from_file(os.path.basename(path)) == sym


def test_day_number_to_date():
    assert br.ist_date(OPEN) == "2026-10-19"
    assert br.ist_date(DAYNUM * 86400 - 19800 + 43200) == "2026-10-19"


def test_write_then_unchanged_then_one_more_bar_rewrites():
    root = config.BAR_RECORD_DIR
    b = _bars(OPEN, 30)
    size = br.write_symbol(root, "2026-10-19", "TCS", b)
    assert size and size > 100
    doc = json.load(open(br.file_for(root, "2026-10-19", "TCS")))
    assert doc["v"] == 1 and doc["n"] == 30 and doc["tf"] == 1 and doc["first_ts"] == OPEN and doc["backfilled"] is False and len(doc["bars"]) == 30
    assert br.write_symbol(root, "2026-10-19", "TCS", b) is None
    assert br.write_symbol(root, "2026-10-19", "TCS", _bars(OPEN, 31)) is not None
    assert json.load(open(br.file_for(root, "2026-10-19", "TCS")))["n"] == 31
    assert not [f for f in os.listdir(os.path.join(root, "2026-10-19")) if f.startswith(".tmp_")]


def test_too_few_bars_and_bad_rows_are_not_written():
    root = config.BAR_RECORD_DIR
    assert br.write_symbol(root, "2026-10-19", "X", _bars(OPEN, 10)) is None
    assert not os.path.exists(br.file_for(root, "2026-10-19", "X"))
    junk = _bars(OPEN, 25) + [(0, 1, 1, 1, 1, 1), ("a",), (OPEN + 999, 1, 0.5, 2, 1, 1), (OPEN + 1999, 100, 101, 99, 100, float("nan"))]
    br.write_symbol(root, "2026-10-19", "J", junk)
    assert json.load(open(br.file_for(root, "2026-10-19", "J")))["n"] == 25


def test_restart_never_shrinks_a_recorded_day_and_memory_wins_on_the_same_minute():
    root = config.BAR_RECORD_DIR
    br.write_symbol(root, "2026-10-19", "INFY", _bars(OPEN, 60))
    br.reset()
    br.write_symbol(root, "2026-10-19", "INFY", _bars(OPEN, 30, start=50, px=200.0))
    doc = json.load(open(br.file_for(root, "2026-10-19", "INFY")))
    assert doc["n"] == 80 and doc["first_ts"] == OPEN
    by_t = {b[0]: b for b in doc["bars"]}
    assert by_t[OPEN + 55 * 60][1] == round(200.0 + 5 * 0.01, 4)
    assert by_t[OPEN + 10 * 60][1] == round(100.0 + 10 * 0.01, 4)
    assert br.stats()["merged_existing"] == 1


def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp_file():
    root = config.BAR_RECORD_DIR
    br.write_symbol(root, "2026-10-19", "SBIN", _bars(OPEN, 30))
    real = os.replace

    def boom(a, b):
        raise OSError("disk full")
    os.replace = boom
    try:
        try:
            br.write_symbol(root, "2026-10-19", "SBIN", _bars(OPEN, 40))
            raise AssertionError("should have raised")
        except OSError:
            pass
    finally:
        os.replace = real
    assert json.load(open(br.file_for(root, "2026-10-19", "SBIN")))["n"] == 30
    assert not [f for f in os.listdir(os.path.join(root, "2026-10-19")) if f.startswith(".tmp_")]


def _inject(store):
    return dict(bars_fn=lambda s: store[s], symbols_fn=lambda: list(store), day_fn=_day_fn, backfilled_fn=lambda s: False)


def test_run_once_cadence_and_closed_market():
    store = {"AAA": _bars(OPEN, 40), "BBB": _bars(OPEN, 40)}
    d1 = br.run_once(OPEN + 3600, True, **_inject(store))
    assert d1["wrote"] == 2 and d1["skipped"] is None
    assert br.run_once(OPEN + 3700, True, **_inject(store))["skipped"] == "interval"
    store["AAA"] = _bars(OPEN, 41)
    d3 = br.run_once(OPEN + 3600 + 301, True, **_inject(store))
    assert d3["wrote"] == 1 and d3["unchanged"] == 1
    assert br.run_once(_ts(2026, 10, 19, 8, 0), False, **_inject(store))["skipped"] == "closed"
    assert br.run_once(_ts(2026, 10, 19, 17, 0), False, **_inject(store))["skipped"] == "closed"
    assert br.run_once(_ts(2026, 10, 24, 15, 45), False, **_inject(store))["skipped"] == "closed"


def test_final_flush_after_the_close_happens_once_per_day():
    store = {"AAA": _bars(OPEN, 375)}
    br.run_once(_ts(2026, 10, 19, 15, 20), True, **_inject(store))
    store["AAA"] = _bars(OPEN, 375)
    store["AAA"][-1] = (store["AAA"][-1][0], 100, 101, 99, 100.5, 999.0)
    d = br.run_once(_ts(2026, 10, 19, 15, 40), False, **_inject(store))
    assert d["final"] is True and d["wrote"] == 1
    assert br.run_once(_ts(2026, 10, 19, 15, 50), False, **_inject(store))["skipped"] == "final_done"
    assert json.load(open(br.file_for(config.BAR_RECORD_DIR, "2026-10-19", "AAA")))["bars"][-1][5] == 999.0


def test_disabled_flag_and_unwritable_directory():
    config.BAR_RECORD_ENABLED = False
    assert br.run_once(OPEN + 3600, True, **_inject({"A": _bars(OPEN, 40)}))["skipped"] == "disabled"
    config.BAR_RECORD_ENABLED = True
    blocker = os.path.join(config.BAR_RECORD_DIR, "afile")
    open(blocker, "w").write("x")
    config.BAR_RECORD_DIR = os.path.join(blocker, "sub")
    d = br.run_once(OPEN + 3600, True, **_inject({"A": _bars(OPEN, 40)}))
    assert d["skipped"] == "unwritable" and br.stats()["disabled_reason"]
    assert br.run_once(OPEN + 9000, True, **_inject({"A": _bars(OPEN, 40)}))["skipped"] == "disabled_reason"


def test_symbol_cap_keeps_the_most_liquid():
    config.BAR_RECORD_MAX_SYMBOLS = 10
    store = {f"S{k:02d}": _bars(OPEN, 25) for k in range(14)}
    d = br.run_once(OPEN + 3600, True, **_inject(store))
    assert d["wrote"] == 10 and br.stats()["symbols_last"] == 10


def test_prune_removes_only_old_date_folders():
    root = config.BAR_RECORD_DIR
    for name in ("2026-08-01", "2026-09-01", "2026-10-18", "keepme", "2026-8-1"):
        os.makedirs(os.path.join(root, name))
        open(os.path.join(root, name, "A.json"), "w").write("{}")
    assert br.prune_days(root, 45, "2026-10-19") == 2
    assert sorted(os.listdir(root)) == ["2026-10-18", "2026-8-1", "keepme"]


def test_history_minutes_is_ascending_across_days_and_excludes_before_date():
    root = config.BAR_RECORD_DIR
    o1, o2, o3 = _ts(2026, 10, 15, 9, 15), _ts(2026, 10, 16, 9, 15), _ts(2026, 10, 19, 9, 15)
    for d, o in (("2026-10-15", o1), ("2026-10-16", o2), ("2026-10-19", o3)):
        br.write_symbol(root, d, "ITC", _bars(o, 25))
    got = br.history_minutes("ITC", 5, root=root, before_date="2026-10-19")
    assert len(got) == 50 and got == sorted(got, key=lambda b: b[0]) and got[0][0] == o1
    assert len(br.history_minutes("ITC", 1, root=root, before_date="2026-10-19")) == 25
    assert br.history_minutes("NOPE", 5, root=root) == [] and br.history_minutes("ITC", 5, root="/does/not/exist") == []


def test_board_runner_calls_the_recorder_and_its_failure_never_counts_as_a_runner_error():
    from screening import board_runner
    board_runner.reset()
    calls = []
    config.BAR_RECORD_ENABLED = True
    out = board_runner.run_once(OPEN + 100, False, open_symbols_fn=lambda: [], session_factory=lambda: None, record_fn=lambda n, m: calls.append((n, m)) or {"wrote": 3})
    assert calls == [(OPEN + 100, False)] and out["recorded"] == {"wrote": 3}

    def bad(n, m):
        raise RuntimeError("disk gone")
    out2 = board_runner.run_once(OPEN + 200, False, open_symbols_fn=lambda: [], session_factory=lambda: None, record_fn=bad)
    assert "recorded" not in out2 and board_runner.state()["errors"] == 0
    config.BAR_RECORD_ENABLED = False
    assert "recorded" not in board_runner.run_once(OPEN + 300, False, open_symbols_fn=lambda: [], session_factory=lambda: None, record_fn=lambda n, m: {"x": 1})
    board_runner.reset()


def test_stats_reports_days_on_disk():
    root = config.BAR_RECORD_DIR
    br.write_symbol(root, "2026-10-16", "A", _bars(OPEN, 25))
    br.write_symbol(root, "2026-10-19", "A", _bars(OPEN, 25))
    br.write_symbol(root, "2026-10-19", "B", _bars(OPEN, 25))
    st = br.stats()
    assert st["days_on_disk"] == 2 and st["first_day"] == "2026-10-16" and st["last_day"] == "2026-10-19" and st["last_day_files"] == 2
