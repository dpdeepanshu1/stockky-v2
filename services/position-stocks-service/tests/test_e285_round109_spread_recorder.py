"""E285 round 109: the per-minute spread recorder (feed/spread_recorder), its hooks in the md poller / depth gate / board runner.
Plain-function runner: python3 tests/test_e285_round109_spread_recorder.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import bar_recorder, spread_recorder as sr

_HERE = os.path.dirname(os.path.abspath(__file__))
D = "2026-10-28"
T0 = 1793000000.0 - (1793000000 % 60)          # a minute boundary
DAY_TS = None


def _setup():
    root = tempfile.mkdtemp(prefix="spread_rec_")
    config.SPREAD_RECORD_ENABLED = True
    config.BAR_RECORD_DIR = root
    config.SPREAD_RECORD_EVERY_S = 60
    config.SPREAD_RECORD_KEEP_DAYS = 45
    sr.reset()
    return root


def _teardown(root):
    config.SPREAD_RECORD_ENABLED = False
    sr.reset()
    shutil.rmtree(root, ignore_errors=True)


def test_median_and_max_per_minute():
    root = _setup()
    try:
        for i, v in enumerate([0.10, 0.30, 0.20]):
            assert sr.note("AAA", v, T0 + i)
        sr.note("AAA", 0.5, T0 + 61)                       # next minute closes the first
        rows = list(sr._done["AAA"].values())
        assert rows == [[T0, 0.2, 0.3, 3]], rows
    finally:
        _teardown(root)


def test_bad_readings_are_not_counted():
    root = _setup()
    try:
        for bad in (None, "x", 0, -1, float("nan"), 99.0):
            assert sr.note("AAA", bad, T0) is False
        assert sr.note("^NSEI", 0.1, T0) is False and sr.note("", 0.1, T0) is False
        assert sr._cur == {}
        assert sr.stats()["rejected"] >= 3
        assert sr.note("RELIANCE.NS", 0.1, T0) and "RELIANCE" in sr._cur           # the suffix is dropped
    finally:
        _teardown(root)


def test_switch_off_records_nothing_and_late_reading_is_ignored():
    root = _setup()
    try:
        sr.note("AAA", 0.1, T0 + 70)
        assert sr.note("AAA", 0.1, T0) is False            # an older minute than the open one
        config.SPREAD_RECORD_ENABLED = False
        assert sr.note("BBB", 0.1, T0) is False and "BBB" not in sr._cur
        assert sr.run_once(T0, True)["skipped"] == "disabled"
    finally:
        _teardown(root)


def test_caps_on_samples_and_symbols():
    root = _setup()
    try:
        config.SPREAD_RECORD_MAX_SAMPLES = 3
        for i in range(10):
            sr.note("AAA", 0.1 + i / 100.0, T0 + i)
        assert len(sr._cur["AAA"][1]) == 3
        config.SPREAD_RECORD_MAX_SYMBOLS = 2
        sr.note("BBB", 0.1, T0)
        assert sr.note("CCC", 0.1, T0) is False and sr.stats()["dropped_symbols"] == 1
    finally:
        config.SPREAD_RECORD_MAX_SAMPLES, config.SPREAD_RECORD_MAX_SYMBOLS = 30, 600
        _teardown(root)


def test_write_layout_beside_bar_folders_and_merge():
    root = _setup()
    try:
        sr.note("AAA", 0.2, T0)
        sr.note("AAA", 0.4, T0 + 60)
        sr.note("AAA", 0.6, T0 + 120)                      # minutes 0 and 1 are closed
        date = sr.ist_date(T0)
        did = sr.run_once(T0 + 125, True)
        assert did["wrote"] == 1, did
        p = sr.file_for(root, date, "AAA")
        assert p == os.path.join(root, "spread", date, "AAA.json") and os.path.exists(p)
        doc = json.load(open(p))
        assert doc["n"] == 2 and doc["rows"][0] == [T0, 0.2, 0.2, 1], doc
        assert sr.run_once(T0 + 130, True)["skipped"] == "interval"
        assert not any(n == date for n in os.listdir(root))      # nothing inside a bar day folder: the bar recorder never sees it
        assert bar_recorder.stats()["days_on_disk"] == 0
        # a restart: memory is empty, the first write merges with the file and keeps the old minutes
        sr.reset(); config.SPREAD_RECORD_ENABLED = True
        sr.note("AAA", 0.9, T0 + 180); sr.note("AAA", 0.9, T0 + 240)
        assert sr.run_once(T0 + 245, True)["wrote"] == 1
        doc = json.load(open(p))
        assert [r[0] for r in doc["rows"]] == [T0, T0 + 60, T0 + 180], doc["rows"]
        assert sr.stats()["merged_existing"] == 1
    finally:
        _teardown(root)


def test_final_flush_after_close_writes_the_open_minute_once():
    root = _setup()
    try:
        import calendar
        close = calendar.timegm((2026, 10, 28, 15, 31, 0)) - 19800            # Wed 15:31 IST
        sr.note("AAA", 0.3, close - 600)
        sr.note("AAA", 0.5, close - 90)
        did = sr.run_once(close, False)
        assert did["final"] is True and did["wrote"] == 1
        doc = json.load(open(sr.file_for(root, sr.ist_date(close), "AAA")))
        assert doc["n"] == 2
        assert sr.run_once(close + 60, False)["skipped"] == "final_done"
        assert sr.run_once(close - 4 * 3600, False)["skipped"] in ("closed", "final_done")
    finally:
        _teardown(root)


def test_unwritable_directory_switches_off_with_a_reason():
    root = _setup()
    try:
        blocker = os.path.join(root, "file")
        open(blocker, "w").close()
        config.BAR_RECORD_DIR = blocker                    # a file, not a folder
        sr.note("AAA", 0.1, T0); sr.note("AAA", 0.1, T0 + 60)
        did = sr.run_once(T0 + 100, True)
        assert did["skipped"] == "unwritable" and sr.stats()["disabled_reason"]
        assert sr.run_once(T0 + 700, True)["skipped"] == "disabled_reason"
    finally:
        _teardown(root)


def test_prune_only_old_date_folders_under_spread():
    root = _setup()
    try:
        for d in ("2026-08-01", "2026-10-27", "not-a-date"):
            os.makedirs(os.path.join(root, "spread", d))
            open(os.path.join(root, "spread", d, "A.json"), "w").close()
        os.makedirs(os.path.join(root, "2026-08-01"))              # a BAR day folder is never touched
        assert sr.prune_days(root, 45, "2026-10-28") == 1
        left = sorted(os.listdir(os.path.join(root, "spread")))
        assert left == ["2026-10-27", "not-a-date"], left
        assert os.path.isdir(os.path.join(root, "2026-08-01"))
    finally:
        _teardown(root)


def test_history_reads_days_back_in_order():
    root = _setup()
    try:
        for i, d in enumerate(("2026-10-26", "2026-10-27", "2026-10-28")):
            p = sr.file_for(root, d, "AAA")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            json.dump({"rows": [[T0 + i * 86400, 0.1 * (i + 1), 0.5, 2]]}, open(p, "w"))
        h = sr.history("AAA", days=2, root=root)
        assert [r[1] for r in h] == [0.2, 0.3], h
        assert [r[1] for r in sr.history("AAA", days=5, root=root, before_date="2026-10-28")] == [0.1, 0.2]
        assert sr.history("NOPE", root=root) == [] and sr.history("AAA", root="/no/such/dir") == []
    finally:
        _teardown(root)


def test_md_poller_row_feeds_the_recorder():
    root = _setup()
    try:
        try:
            from feed import md_poller
        except ModuleNotFoundError as e:                   # a sandbox without the websocket package: the wiring is still pinned by test_wiring_by_source_text
            print("     (skipped: %s)" % e)
            return
        saved = md_poller.ws_client._ingest_tick
        md_poller.ws_client._ingest_tick = lambda *a, **k: None
        try:
            md_poller.reset()
            import time as _t
            now = _t.time()
            n = md_poller.ingest_rows([{"symbol": "AAA", "price": 100.0, "volume": 10, "spread_pct": 0.12, "fetched_at": None},
                                       {"symbol": "BBB", "price": 50.0, "volume": 10}], now)
        finally:
            md_poller.ws_client._ingest_tick = saved
            md_poller.reset()
        assert n == 2
        assert "AAA" in sr._cur and "BBB" not in sr._cur            # no spread on the row = nothing recorded, never a guess
        st = sr.stats()
        assert st["rows_seen"] == 2 and st["rows_with_spread"] == 1
    finally:
        _teardown(root)


def test_wiring_by_source_text():
    def src(*p):
        return open(os.path.join(_HERE, "..", *p), encoding="utf-8").read()
    assert "spread_recorder.note(symbol, body.get(\"spread_pct\"))" in src("orders", "depth_gate.py")
    assert "spread_recorder.note(sym, row.get(\"spread_pct\"), ts)" in src("feed", "md_poller.py")
    assert "spread_recorder.run_once(now, market_open)" in src("screening", "board_runner.py")
    assert "_spread_record_status()" in src("main.py")
    assert "SPREAD_RECORD_ENABLED" in src("control", "catalog.py") and "SPREAD_RECORD_ENABLED" in src("config.py")


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
