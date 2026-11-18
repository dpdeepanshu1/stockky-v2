"""Round 610: the Real Trade 1-minute bar cache serves the names the entry walk asks for first (entry_engine/orb_bars.py).

Plain runner: `python3 tests/test_round610_orb_bars_priority.py` (also collected by pytest). The fetch is a fake that blocks on an Event so slots stay busy."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from entry_engine import orb_bars as OB  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 10, 26, tzinfo=IST)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def _row(h, m):
    return {"date": f"2026-10-26 {h:02d}:{m:02d}", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000}


class _Rig:
    """FETCH_ON, a blocking fake fetch, and small limits (MAX_INFLIGHT 4, reserve 2) for one test; restores everything."""

    def __init__(self, max_inflight=4, reserve=2):
        self.mi, self.rs = max_inflight, reserve
        self.gate = threading.Event()
        self.calls: list = []

    def __enter__(self):
        OB._reset_for_tests()
        self.saved = (OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE, OB.WANT_TTL_S)
        OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE = True, self.mi, self.rs

        def fake(sym):
            self.calls.append(sym)
            self.gate.wait(3.0)
            return [_row(9, 15)]
        OB._fetch = fake
        return self

    def __exit__(self, *a):
        self.gate.set()
        time.sleep(0.05)
        OB.FETCH_ON, OB.MAX_INFLIGHT, OB.PRIORITY_RESERVE, OB.WANT_TTL_S = self.saved
        OB._reset_for_tests()

    def wait(self, cond, secs=2.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            if cond():
                return True
            time.sleep(0.01)
        return cond()


def test_defaults_are_six_slots_two_reserved():
    assert OB.MAX_INFLIGHT == 6 and OB.PRIORITY_RESERVE == 2 and OB.WANT_TTL_S == 30.0


def test_record_only_callers_leave_the_reserve_free():
    with _Rig(4, 2) as r:
        for s in ("A", "B", "C", "D", "E"):
            OB.get_bars(s, now=at(9, 30))                    # the recorder: default priority
        assert r.wait(lambda: len(r.calls) == 2)
        time.sleep(0.05)
        assert len(OB._inflight) == 2 and len(r.calls) == 2   # only MAX_INFLIGHT - RESERVE = 2 slots used, and not queued
        assert not OB._wanted
        OB.get_bars("PRIO1", now=at(9, 30), priority=True)
        OB.get_bars("PRIO2", now=at(9, 30), priority=True)
        assert r.wait(lambda: len(r.calls) == 4) and {"PRIO1", "PRIO2"} <= set(r.calls)


def test_priority_name_with_no_free_slot_is_queued_and_served_when_a_slot_frees():
    with _Rig(2, 1) as r:
        OB.get_bars("P1", now=at(9, 30), priority=True)
        OB.get_bars("P2", now=at(9, 30), priority=True)
        assert r.wait(lambda: len(r.calls) == 2)
        OB.get_bars("P3", now=at(9, 30), priority=True)       # both slots busy
        assert len(OB._wanted) == 1 and OB.stats()["queued"] == 1 and "P3" not in r.calls
        r.gate.set()                                          # the fetches finish; a worker takes P3 itself
        assert r.wait(lambda: "P3" in r.calls)
        assert r.wait(lambda: OB.get_bars("P3", now=at(9, 30), priority=True) is not None)
        assert not OB._wanted


def test_queue_serves_the_oldest_ask_first_and_drops_old_ones():
    with _Rig(1, 0) as r:
        OB.get_bars("P1", now_mono=1000.0, now=at(9, 30), priority=True)
        assert r.wait(lambda: r.calls == ["P1"])
        OB.get_bars("Q1", now_mono=1001.0, now=at(9, 30), priority=True)
        OB.get_bars("Q2", now_mono=1002.0, now=at(9, 30), priority=True)
        OB.get_bars("OLD", now_mono=900.0, now=at(9, 30), priority=True)   # asked long ago
        assert set(k[0] for k in OB._wanted) == {"Q1", "Q2", "OLD"}
        first = OB._next_wanted(mono=1003.0)
        assert first is not None and first[0] == "Q1"          # OLD (age 103 s > 30 s) is dropped, Q1 is the oldest still fresh
        assert OB.stats()["queue_dropped"] == 1
        OB._inflight.discard(first)
        assert OB._next_wanted(mono=1003.0)[0] == "Q2"


def test_default_calls_are_never_queued():
    with _Rig(1, 0) as r:
        OB.get_bars("P1", now=at(9, 30))
        assert r.wait(lambda: r.calls == ["P1"])
        OB.get_bars("LOW", now=at(9, 30))
        assert not OB._wanted and OB.stats()["queued"] == 0


def test_a_name_with_the_last_closable_bar_cached_is_not_fetched_again():
    with _Rig(4, 2) as r:
        r.gate.set()
        now = at(9, 31, 20)                                  # the last bar that can have closed is 09:30 (end 09:31:00)
        key = ("UPTODATE", "2026-10-26")
        OB._cache[key] = {"bars": [(at(9, 30).timestamp(), 100, 100, 100, 100, 1000)], "at": time.monotonic()}
        OB.get_bars("UPTODATE", now_mono=5000.0, now=now, priority=True)
        time.sleep(0.05)
        assert r.calls == [] and OB.stats()["skipped_current"] == 1 and OB.stats()["requests"] == 0
        OB._cache[key] = {"bars": [(at(9, 28).timestamp(), 100, 100, 100, 100, 1000)], "at": time.monotonic()}   # lagging bars: a refetch is due
        OB.get_bars("UPTODATE", now_mono=5001.0, now=now, priority=True)
        assert r.wait(lambda: r.calls == ["UPTODATE"])


def test_a_symbol_in_flight_or_inside_the_refresh_window_is_not_queued_twice():
    with _Rig(1, 0) as r:
        OB.get_bars("P1", now_mono=100.0, now=at(9, 30), priority=True)
        assert r.wait(lambda: r.calls == ["P1"])
        OB.get_bars("P1", now_mono=101.0, now=at(9, 30), priority=True)      # in flight
        OB.get_bars("Z", now_mono=102.0, now=at(9, 30), priority=True)
        OB.get_bars("Z", now_mono=103.0, now=at(9, 30), priority=True)       # already queued
        assert len(OB._wanted) == 1 and OB.stats()["queued"] == 1


def test_fetch_switch_off_still_starts_and_queues_nothing():
    with _Rig(4, 2) as r:
        OB.FETCH_ON = False
        OB.get_bars("X", now=at(9, 30), priority=True)
        assert not OB._wanted and OB.stats()["requests"] == 0 and r.calls == []


def test_log_line_format_is_unchanged_by_the_new_counters():
    OB._reset_for_tests()
    OB.log_stats_if_due(now_mono=0.0)
    OB._stats.update(hit=6, miss=3, stale=1, ok=4, failed=1, queued=9, skipped_current=5)
    line = OB.log_stats_if_due(now_mono=300.0)
    assert line == ("ORB bars, last 300s: 10 decision(s): hit 6, miss 3, stale 1 (60% carried the close-trigger fields); bar fetches ok 4, failed 1")
    OB._reset_for_tests()


def test_wiring_priority_callers_and_the_recorder_stays_default():
    root = HERE
    ent = (root / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert "orb_bars.get_bars(s, priority=True)" in ent and "orb_bars.get_bars(cand.symbol, priority=True)" in ent
    assert "priority=True" in (root / "swing" / "live.py").read_text(encoding="utf-8")
    ex = (root / "exit_engine" / "exit.py").read_text(encoding="utf-8")
    assert "_ob.get_bars(position.symbol, priority=True)" in ex and "_ob_pl.get_bars(position.symbol, priority=True)" in ex
    shadow = (root / "entry_engine" / "orb_shadow.py").read_text(encoding="utf-8")
    assert "priority" not in shadow                          # fields_for -> get_bars(symbol, now=now): the recorder stays low priority


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
