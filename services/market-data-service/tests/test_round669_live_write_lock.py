"""Round 669 (E691, D2b): the AngelOne and Dhan live_quotes writers no longer overlap, and send rows in symbol order.

Run from services/market-data-service:   python3 -m pytest tests/test_round669_live_write_lock.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import importlib

import angelone_ws_feed as f
import live_write_lock as lwl
from dhan_data import live_store as ls


class _Probe:
    """Fake engine: records every executed param list and the highest number of transactions open at once."""

    def __init__(self, hold_s: float = 0.0):
        self.hold_s = hold_s
        self.calls = []
        self.open_now = 0
        self.max_open = 0
        self._lock = threading.Lock()

    def begin(self):
        return _Tx(self)


class _Tx:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        with self.eng._lock:
            self.eng.open_now += 1
            self.eng.max_open = max(self.eng.max_open, self.eng.open_now)
        return self

    def execute(self, stmt, params):
        self.eng.calls.append(list(params) if isinstance(params, list) else [params])
        if self.eng.hold_s:
            time.sleep(self.eng.hold_s)

    def __exit__(self, *a):
        with self.eng._lock:
            self.eng.open_now -= 1
        return False


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Other test modules pop angelone_ws_feed from sys.modules and re-import it; live_store imports it at call time, so use the live one.
    global f
    f = importlib.import_module("angelone_ws_feed")
    for k in ("LIVE_QUOTES_SINGLE_WRITER", "LIVE_QUOTES_WRITE_LOCK_WAIT_S", "LIVE_QUOTES_RETRY_MAX_AGE_S",
              "ANGELONE_FEED_DB_CHANGE_ONLY", "DHAN_LIVE_WRITE_BACKOFF_S"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_LIVE_WRITE", "on")
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0")
    monkeypatch.setenv("DHAN_LIVE_WRITE_CHUNK", "100")
    f._RETRY.clear(); ls._RETRY.clear()
    f._AO_LAST_SENT.clear()
    for k in list(ls._stats):
        ls._stats[k] = None if k == "last_error" else 0
    ls._last_sent.clear(); ls._latest.clear(); ls._prio.clear()
    ls._backoff_until = 0.0
    for k in lwl._STATS:
        lwl._STATS[k] = 0 if k in ("acquired", "busy") else 0.0
    yield


def _use(monkeypatch, eng):
    monkeypatch.setattr(f, "_get_live_quotes_engine", lambda: (eng, "oracle"))
    monkeypatch.setattr(f, "_ensure_schema", lambda e, d: None)


def _ao_rows(syms):
    return [(s, 100.0 + i, 99.0, 101.0, 98.0, 99.5, 10) for i, s in enumerate(syms)]


def _dhan_rows(syms):
    return [{"symbol": s, "price": 100.0 + i, "previous_close": 99.0, "open": 99.0, "day_high": 101.0,
             "day_low": 98.0, "volume": 10} for i, s in enumerate(syms)]


class TestLockModule:
    def test_two_threads_never_hold_the_slot_together(self):
        inside, peak = [0], [0]
        guard = threading.Lock()

        def work():
            for _ in range(5):
                with lwl.write_slot(wait=5):
                    with guard:
                        inside[0] += 1
                        peak[0] = max(peak[0], inside[0])
                    time.sleep(0.005)
                    with guard:
                        inside[0] -= 1

        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert peak[0] == 1
        assert lwl.status()["acquired"] == 20

    def test_busy_slot_raises_after_the_wait_without_timeout_words(self):
        with lwl.write_slot(wait=1):
            t0 = time.monotonic()
            with pytest.raises(lwl.WriteSlotBusy) as ei:
                with lwl.write_slot(wait=0.1):
                    pass
            assert time.monotonic() - t0 < 1.0
        txt = f"{type(ei.value).__name__} {ei.value}".lower()
        assert "timeout" not in txt and "timed out" not in txt and "dpy-4024" not in txt
        assert lwl.status()["busy"] == 1
        with lwl.write_slot(wait=0.1):      # released after the failed attempt and after the with-block
            pass

    def test_slot_is_released_when_the_body_raises(self):
        with pytest.raises(RuntimeError):
            with lwl.write_slot(wait=1):
                raise RuntimeError("boom")
        assert not lwl._LOCK.locked()

    @pytest.mark.parametrize("val", ["0", "false", "off", "no", "NO"])
    def test_switch_off_means_no_lock(self, monkeypatch, val):
        monkeypatch.setenv("LIVE_QUOTES_SINGLE_WRITER", val)
        with lwl.write_slot(wait=0.1):
            with lwl.write_slot(wait=0.1):      # would raise if it were locking
                pass
        assert lwl.status()["acquired"] == 0 and lwl.status()["enabled"] is False

    def test_wait_setting_defaults_and_bounds(self, monkeypatch):
        assert lwl.wait_s() == 6.0
        monkeypatch.setenv("LIVE_QUOTES_WRITE_LOCK_WAIT_S", "abc")
        assert lwl.wait_s() == 6.0
        monkeypatch.setenv("LIVE_QUOTES_WRITE_LOCK_WAIT_S", "0")
        assert lwl.wait_s() == 0.5
        monkeypatch.setenv("LIVE_QUOTES_WRITE_LOCK_WAIT_S", "999")
        assert lwl.wait_s() == 30.0

    def test_ordered_handles_tuples_dicts_and_key(self):
        assert [r[0] for r in lwl.ordered([("B",), ("A",), ("C",)])] == ["A", "B", "C"]
        assert [r["s"] for r in lwl.ordered([{"s": "Z"}, {"s": "M"}])] == ["M", "Z"]
        assert [r["symbol"] for r in lwl.ordered([{"symbol": "Z"}, {"symbol": "M"}])] == ["M", "Z"]
        assert lwl.ordered([3, 1, 2], key=lambda x: x) == ["1", "2", "3"] or lwl.ordered([3, 1, 2], key=lambda x: x) == [1, 2, 3]
        assert lwl.ordered([]) == []


class TestAngelOneWriter:
    def test_rows_are_sent_in_symbol_order(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        assert f._upsert_ticks_batch_sync(_ao_rows(["ZED", "ABB", "MID", "CAT"])) is True
        assert [p["s"] for p in eng.calls[0]] == ["ABB", "CAT", "MID", "ZED"]

    def test_busy_slot_fails_the_write_but_does_not_open_the_backoff(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        monkeypatch.setenv("LIVE_QUOTES_WRITE_LOCK_WAIT_S", "0.5")
        monkeypatch.setattr(lwl, "wait_s", lambda: 0.05)
        with lwl.write_slot(wait=1):
            assert f._upsert_ticks_batch_sync(_ao_rows(["A", "B"])) is False
        assert eng.calls == []
        assert ls.in_backoff() is False
        assert f._upsert_ticks_batch_sync(_ao_rows(["A", "B"])) is True      # free again: written

    def test_single_row_writer_also_takes_the_slot(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        seen = []
        orig = lwl.write_slot

        def spy(*a, **k):
            seen.append(1)
            return orig(*a, **k)

        monkeypatch.setattr(lwl, "write_slot", spy)
        f._upsert_tick_sync("ABB", 10.0, 1, 2, 3, 4, 5)
        assert seen == [1] and len(eng.calls) == 1


class TestDhanWriter:
    def test_chunk_rows_are_sent_in_symbol_order(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        n = ls._write_sync(_dhan_rows(["ZED", "ABB", "MID", "CAT"]))
        assert n == 4
        assert [p["s"] for p in eng.calls[0]] == ["ABB", "CAT", "MID", "ZED"]

    def test_busy_slot_sends_chunk_to_retry_and_opens_no_backoff(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        monkeypatch.setattr(lwl, "wait_s", lambda: 0.05)
        with lwl.write_slot(wait=1):
            n = ls._write_sync(_dhan_rows(["A", "B", "C"]))
        assert n == 0 and eng.calls == []
        assert len(ls._RETRY) == 3
        assert ls.in_backoff() is False and ls._stats["timeouts"] == 0
        assert ls._stats["failed_chunks"] == 1

    def test_retry_pass_is_ordered_and_locked(self, monkeypatch):
        eng = _Probe(); _use(monkeypatch, eng)
        ls._RETRY.add((p["s"], p) for p in ls.build_params(_dhan_rows(["ZED", "ABB", "CAT"])))
        assert ls.retry_failed() == 3
        assert [p["s"] for p in eng.calls[0]] == ["ABB", "CAT", "ZED"]
        assert lwl.status()["acquired"] == 1

    def test_status_shows_the_write_lock(self, monkeypatch):
        st = ls.status()
        assert st["write_lock"]["enabled"] is True and "busy" in st["write_lock"]


class TestBothWritersTogether:
    def test_transactions_of_the_two_writers_never_overlap(self, monkeypatch):
        eng = _Probe(hold_s=0.02); _use(monkeypatch, eng)
        syms = [f"S{i:03d}" for i in range(30)]
        errs = []

        def ao():
            for _ in range(4):
                if f._upsert_ticks_batch_sync(_ao_rows(syms)) is not True:
                    errs.append("ao")

        def dh():
            for _ in range(4):
                if ls._write_sync(_dhan_rows(list(reversed(syms)))) != 30:
                    errs.append("dhan")

        ts = [threading.Thread(target=ao), threading.Thread(target=dh)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert errs == []
        assert eng.max_open == 1
        assert len(eng.calls) == 8
        assert all([p["s"] for p in c] == sorted(p["s"] for p in c) for c in eng.calls)

    def test_without_the_lock_they_do_overlap(self, monkeypatch):
        """Guards the test above: with LIVE_QUOTES_SINGLE_WRITER=0 the same run does overlap, so the fake engine can see it."""
        monkeypatch.setenv("LIVE_QUOTES_SINGLE_WRITER", "0")
        eng = _Probe(hold_s=0.05); _use(monkeypatch, eng)
        syms = [f"S{i:03d}" for i in range(10)]
        ts = [threading.Thread(target=lambda: f._upsert_ticks_batch_sync(_ao_rows(syms))),
              threading.Thread(target=lambda: ls._write_sync(_dhan_rows(syms)))]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert eng.max_open == 2
