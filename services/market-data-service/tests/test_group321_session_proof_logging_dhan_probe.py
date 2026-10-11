"""group321: verdict changes are logged; Dhan rows feed the proof; the Nifty probe has its own thread."""
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import session_proof
from dhan_data import live_poller

IST = ZoneInfo("Asia/Kolkata")


def _at(h, m, s=0):
    return datetime(2026, 10, 12, h, m, s, tzinfo=IST)


def setup_function(_f):
    session_proof.reset_state()
    session_proof.reset_nifty_probe()
    session_proof.register_nifty_fetcher(None)


def test_closed_and_overturned_are_logged_as_warnings(caplog):
    caplog.set_level(logging.INFO, logger="session-proof")
    for i in range(12):
        session_proof.observe(f"S{i}", 100.0 + i, _at(9, 20))
    session_proof.status(_at(9, 30))                       # pending
    session_proof.status(_at(10, 5))                       # closed
    session_proof.observe("S1", 101.5, _at(10, 20))        # a real move after the window
    session_proof.status(_at(10, 21))                      # open again
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == "session-proof"]
    assert any(l == logging.WARNING and "-> closed" in m for l, m in msgs)
    assert any(l == logging.WARNING and "closed -> open" in m for l, m in msgs)


def test_same_status_is_not_logged_twice(caplog):
    caplog.set_level(logging.INFO, logger="session-proof")
    for i in range(12):
        session_proof.observe(f"S{i}", 100.0 + i, _at(9, 20))
    session_proof.status(_at(10, 5)); session_proof.status(_at(10, 6)); session_proof.status(_at(10, 7))
    assert len([r for r in caplog.records if "-> closed" in r.getMessage()]) == 1


def test_dhan_rows_feed_the_proof(monkeypatch):
    seen = []
    monkeypatch.setattr(session_proof, "observe", lambda sym, price, now=None: seen.append((sym, price)))
    live_poller._observe_for_session_proof([{"symbol": "AAA", "price": 10.5}, {"symbol": "^NSEI", "price": 24000.0}])
    assert seen == [("AAA", 10.5), ("^NSEI", 24000.0)]


def test_dhan_observe_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(session_proof, "observe", boom)
    live_poller._observe_for_session_proof([{"symbol": "AAA", "price": 10.5}])
    live_poller._observe_for_session_proof(None)


def test_probe_thread_needs_a_fetcher():
    assert session_proof.start_probe_thread() is None


def test_probe_thread_runs_and_stops(monkeypatch):
    calls = []
    monkeypatch.setattr(session_proof, "probe_nifty", lambda *a, **k: calls.append(1) or False)
    session_proof.register_nifty_fetcher(lambda: 24000.0)
    monkeypatch.setattr(session_proof, "_probe_thread", None)
    stop = session_proof.start_probe_thread(every_s=5.0)
    assert isinstance(stop, threading.Event)
    t0 = time.time()
    while not calls and time.time() - t0 < 2:
        time.sleep(0.02)
    assert calls
    assert session_proof.start_probe_thread() is None      # already running
    stop.set()


def test_probe_thread_can_be_switched_off(monkeypatch):
    session_proof.register_nifty_fetcher(lambda: 24000.0)
    monkeypatch.setattr(session_proof, "NIFTY_THREAD_ENABLED", False)
    monkeypatch.setattr(session_proof, "_probe_thread", None)
    assert session_proof.start_probe_thread() is None
