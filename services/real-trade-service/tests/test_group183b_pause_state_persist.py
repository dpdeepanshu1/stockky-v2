"""group183b (item 7): the dead-symbol pause (group 160) and the no-daily-history pause (group 172) survive a restart.
Run: python3 -m pytest tests/test_group183b_pause_state_persist.py -q"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from market_feed import feed as f
from candidate_engine import candidates as cand
from resilience import pause_state as ps


class _Store:
    """Dict-backed stand-in for resilience.local_cache save_snapshot / load_snapshot."""
    def __init__(self):
        self.data = {}

    def install(self, monkeypatch):
        from resilience import local_cache as lc
        monkeypatch.setattr(lc, "save_snapshot", lambda db, key, payload: self.data.__setitem__(key, payload))
        monkeypatch.setattr(lc, "load_snapshot", lambda db, key: self.data.get(key))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("FEED_DEAD_SKIP", "FEED_DEAD_AFTER_MISSES", "FEED_DEAD_BACKOFF_S", "FEED_DEAD_BACKOFF_MAX_S",
              "PAUSE_STATE_PERSIST", "PAUSE_STATE_FLUSH_DELAY_S"):
        monkeypatch.delenv(k, raising=False)
    f.clear_dead_symbols()
    cand.clear_history_state()
    monkeypatch.setattr(f, "_DEAD_PERSIST_READY", False)
    monkeypatch.setattr(cand, "_HIST_PERSIST_READY", False)
    ps._PENDING.clear()
    yield
    f.clear_dead_symbols()
    cand.clear_history_state()
    ps._PENDING.clear()


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    s.install(monkeypatch)
    return s


@pytest.fixture
def flushes(monkeypatch):
    """Record schedule_flush calls instead of starting timers."""
    calls = []
    monkeypatch.setattr(ps, "schedule_flush", lambda key, builder: calls.append((key, builder)))
    return calls


# ── pause_state helpers ──────────────────────────────────────────────────────────────────────────────────
def test_mono_wall_round_trip():
    m = time.monotonic() + 100
    assert abs(ps.wall_to_mono(ps.mono_to_wall(m)) - m) < 0.5


def test_load_items_keeps_only_future_valid_entries(store):
    now = time.time()
    store.data["k"] = {"v": 1, "items": {
        "LIVE": {"u": now + 600}, "OLD": {"u": now - 5}, "BAD": {"u": "x"}, "NOU": {}, "": {"u": now + 9}, "LST": [1],
    }}
    assert set(ps.load_items(None, "k")) == {"LIVE"}


def test_load_items_missing_or_garbage_snapshot(store):
    assert ps.load_items(None, "nope") == {}
    store.data["k"] = {"items": [1, 2]}
    assert ps.load_items(None, "k") == {}


def test_save_items_caps_to_newest(store, monkeypatch):
    monkeypatch.setattr(ps, "MAX_ITEMS", 2)
    ps.save_items(None, "k", {"A": {"u": 1}, "B": {"u": 3}, "C": {"u": 2}})
    assert set(store.data["k"]["items"]) == {"B", "C"}


def test_save_and_load_never_raise(monkeypatch):
    from resilience import local_cache as lc

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(lc, "save_snapshot", boom)
    monkeypatch.setattr(lc, "load_snapshot", boom)
    ps.save_items(None, "k", {"A": {"u": 1}})
    assert ps.load_items(None, "k") == {}


def test_schedule_flush_is_debounced_and_can_be_switched_off(monkeypatch):
    started = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args, self.daemon = delay, fn, args, False

        def start(self):
            started.append(self)
    monkeypatch.setattr(ps.threading, "Timer", FakeTimer)
    ps.schedule_flush("k", lambda: {})
    ps.schedule_flush("k", lambda: {})
    assert len(started) == 1 and started[0].delay == 5.0 and started[0].daemon is True
    monkeypatch.setenv("PAUSE_STATE_PERSIST", "0")
    ps.schedule_flush("other", lambda: {})
    assert len(started) == 1


def test_flush_run_writes_with_own_session_and_closes_it(store, monkeypatch):
    closed = []

    class Sess:
        def close(self):
            closed.append(1)
    stub = types.ModuleType("db")
    stub.get_session_factory = lambda: (lambda: Sess())
    monkeypatch.setitem(sys.modules, "db", stub)
    ps._PENDING["k"] = object()
    ps._run("k", lambda: {"A": {"u": 5}})
    assert store.data["k"]["items"] == {"A": {"u": 5}} and closed == [1] and "k" not in ps._PENDING


def test_flush_run_survives_a_failing_builder(monkeypatch):
    stub = types.ModuleType("db")
    stub.get_session_factory = lambda: (lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setitem(sys.modules, "db", stub)

    def bad():
        raise RuntimeError("x")
    ps._run("k", bad)          # must not raise


# ── group 160 dead-symbol pause ──────────────────────────────────────────────────────────────────────────
def test_dead_snapshot_has_only_paused_symbols_with_future_deadline():
    for _ in range(3):
        f._note_no_data("QUALIANCE")
    f._note_no_data("ONEMISS")
    snap = f._dead_snapshot()
    assert set(snap) == {"QUALIANCE"}
    assert snap["QUALIANCE"]["m"] == 3
    assert 1700 < snap["QUALIANCE"]["u"] - time.time() <= 1801


def test_nothing_is_scheduled_before_startup_load(flushes):
    for _ in range(3):
        f._note_no_data("QUALIANCE")
    assert flushes == []


def test_pause_start_schedules_a_flush_after_startup_load(store, flushes):
    f.load_dead_symbols_from_db(None)
    for _ in range(3):
        f._note_no_data("QUALIANCE")
    assert flushes and flushes[-1][0] == "market_feed:dead_symbols"
    assert set(flushes[-1][1]()) == {"QUALIANCE"}


def test_restart_restores_a_running_pause_and_drops_expired(store):
    now = time.time()
    store.data["market_feed:dead_symbols"] = {"v": 1, "items": {
        "BMISL": {"m": 4, "u": now + 900}, "GONE": {"m": 3, "u": now - 10}}}
    f.load_dead_symbols_from_db(None)
    assert f._paused_symbols(["BMISL", "GONE", "TCS"]) == ["BMISL"]
    assert f._DEAD["BMISL"][0] == 4                       # miss count kept, so the next backoff keeps doubling
    assert 800 < f._DEAD["BMISL"][1] - time.monotonic() <= 901


def test_restored_pause_ends_at_its_deadline(store):
    store.data["market_feed:dead_symbols"] = {"v": 1, "items": {"BMISL": {"m": 3, "u": time.time() + 0.05}}}
    f.load_dead_symbols_from_db(None)
    assert f._paused_symbols(["BMISL"]) == ["BMISL"]
    time.sleep(0.1)
    assert f._paused_symbols(["BMISL"]) == []


def test_price_clearing_a_saved_pause_schedules_removal_but_unpaused_does_not(store, flushes):
    f.load_dead_symbols_from_db(None)
    for _ in range(3):
        f._note_no_data("QUALIANCE")
    f._note_no_data("ONEMISS")
    flushes.clear()
    f._note_priced("ONEMISS")
    assert flushes == []
    f._note_priced("QUALIANCE")
    assert len(flushes) == 1 and f._dead_snapshot() == {}


def test_dead_switches_off(store, monkeypatch):
    store.data["market_feed:dead_symbols"] = {"v": 1, "items": {"BMISL": {"m": 3, "u": time.time() + 900}}}
    monkeypatch.setenv("FEED_DEAD_SKIP", "0")
    f.load_dead_symbols_from_db(None)
    assert f._DEAD == {} and f._DEAD_PERSIST_READY is False
    monkeypatch.delenv("FEED_DEAD_SKIP")
    monkeypatch.setenv("PAUSE_STATE_PERSIST", "0")
    f.load_dead_symbols_from_db(None)
    assert f._DEAD == {}


def test_dead_load_never_raises(monkeypatch):
    from resilience import local_cache as lc
    monkeypatch.setattr(lc, "load_snapshot", lambda db, key: (_ for _ in ()).throw(RuntimeError("x")))
    f.load_dead_symbols_from_db(None)


# ── group 172 no-history pause ───────────────────────────────────────────────────────────────────────────
def test_hist_snapshot_and_restore(store):
    cand._HIST_NONE_UNTIL["NEWCO"] = time.monotonic() + 3600
    cand._HIST_NONE_UNTIL["OLDCO"] = time.monotonic() - 1
    snap = cand._hist_snapshot()
    assert set(snap) == {"NEWCO"} and 3500 < snap["NEWCO"]["u"] - time.time() <= 3601
    store.data["candidates:nohist"] = {"v": 1, "items": snap}
    cand.clear_history_state()
    cand.load_nohist_from_db(None)
    assert cand._HIST_NONE_UNTIL["NEWCO"] > time.monotonic() + 3000 and cand._HIST_PERSIST_READY is True


def test_hist_flush_only_after_load(store, flushes):
    cand._persist_hist_state()
    assert flushes == []
    cand.load_nohist_from_db(None)
    cand._persist_hist_state()
    assert flushes[0][0] == "candidates:nohist"


def test_hist_ttl_zero_or_switch_off_does_nothing(store, monkeypatch):
    store.data["candidates:nohist"] = {"v": 1, "items": {"NEWCO": {"u": time.time() + 900}}}
    monkeypatch.setattr(cand, "VOLUME_SHOCK_NOHIST_TTL_S", 0.0)
    cand.load_nohist_from_db(None)
    assert cand._HIST_NONE_UNTIL == {}
    monkeypatch.setattr(cand, "VOLUME_SHOCK_NOHIST_TTL_S", 21600.0)
    monkeypatch.setenv("PAUSE_STATE_PERSIST", "0")
    cand.load_nohist_from_db(None)
    assert cand._HIST_NONE_UNTIL == {}


def test_hist_load_never_raises(monkeypatch):
    from resilience import local_cache as lc
    monkeypatch.setattr(lc, "load_snapshot", lambda db, key: (_ for _ in ()).throw(RuntimeError("x")))
    cand.load_nohist_from_db(None)
