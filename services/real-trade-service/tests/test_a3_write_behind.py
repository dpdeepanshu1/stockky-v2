"""A3 tests: write-behind queue for evidence rows (write_behind.py). Pure: fake handlers and fake sessions, no database.

    cd services/real-trade-service
    python -m pytest tests/test_a3_write_behind.py -v
"""
from __future__ import annotations

import time
from types import SimpleNamespace as NS

import write_behind as wb


class FakeSession:
    def __init__(self, log):
        self.log, self.commits, self.rollbacks, self.closed = log, 0, 0, False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _setup(monkeypatch, handler, **env):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    wb.reset_for_tests()
    wb.stop()                       # no background thread in these tests: drain_once() is called by hand
    sessions = []

    def factory():
        s = FakeSession(sessions)
        sessions.append(s)
        return s
    wb.configure(factory)
    wb.register("k", handler)
    return sessions


def _submit_no_worker(kind, payload):
    """submit() without letting the daemon worker race the test."""
    orig = wb._ensure_worker
    wb._ensure_worker = lambda: None
    try:
        return wb.submit(kind, payload)
    finally:
        wb._ensure_worker = orig


def test_disabled_or_unknown_kind_returns_false(monkeypatch):
    _setup(monkeypatch, lambda db, rows: None)
    assert _submit_no_worker("nope", {}) is False
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")
    assert _submit_no_worker("k", {}) is False
    assert wb.status()["queued"] == 0


def test_batch_is_written_in_one_session_and_committed(monkeypatch):
    got = []
    sessions = _setup(monkeypatch, lambda db, rows: got.append(list(rows)), WRITE_BEHIND_BATCH=3)
    for i in range(5):
        assert _submit_no_worker("k", {"i": i}) is True
    assert wb.drain_once() == 3
    assert got == [[{"i": 0}, {"i": 1}, {"i": 2}]]
    assert sessions[0].commits == 1 and sessions[0].closed
    assert wb.drain_once() == 2
    st = wb.status()
    assert st["written"] == 5 and st["queued"] == 0 and st["batches"] == 2


def test_full_queue_drops_the_oldest_row(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: got.extend(rows), WRITE_BEHIND_MAX_QUEUE=10)
    for i in range(13):
        _submit_no_worker("k", {"i": i})
    st = wb.status()
    assert st["queued"] == 10 and st["dropped_full"] == 3
    wb.drain_once(limit=100)
    assert [r["i"] for r in got] == list(range(3, 13))


def test_timeout_requeues_and_opens_backoff(monkeypatch):
    calls = []

    def handler(db, rows):
        calls.append(len(rows))
        raise RuntimeError("DPY-4024 call timeout of 8000 ms exceeded")
    sessions = _setup(monkeypatch, handler, WRITE_BEHIND_BACKOFF_S=30)
    _submit_no_worker("k", {"i": 1})
    _submit_no_worker("k", {"i": 2})
    assert wb.drain_once() == 0
    st = wb.status()
    assert st["queued"] == 2 and st["timeouts"] == 1 and st["backoff_remaining_s"] > 20 and st["dropped_failed"] == 0
    assert sessions[0].rollbacks == 1
    assert wb.drain_once() == 0 and calls == [2]          # back-off: the handler is not called again


def test_non_timeout_failure_retries_twice_then_drops(monkeypatch):
    calls = []

    def handler(db, rows):
        calls.append(len(rows))
        raise ValueError("bad row")
    _setup(monkeypatch, handler)
    _submit_no_worker("k", {"i": 1})
    for _ in range(5):
        wb.drain_once()
    st = wb.status()
    assert calls == [1, 1, 1] and st["queued"] == 0 and st["dropped_failed"] == 1 and "bad row" in st["last_error"]
    assert st["backoff_remaining_s"] == 0


def test_note_timeout_from_a_tick_pauses_writes(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: got.extend(rows), WRITE_BEHIND_BACKOFF_S=30)
    _submit_no_worker("k", {"i": 1})
    wb.note_timeout()
    assert wb.in_backoff() and wb.drain_once() == 0 and got == []
    monkeypatch.setenv("WRITE_BEHIND_BACKOFF_S", "0")
    wb.reset_for_tests()
    wb.note_timeout()
    assert not wb.in_backoff()                              # 0 = never back off


def test_backoff_zero_keeps_writing_after_timeout(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: got.extend(rows), WRITE_BEHIND_BACKOFF_S=0)
    _submit_no_worker("k", {"i": 1})
    wb.note_timeout()
    assert wb.drain_once() == 1 and got == [{"i": 1}]


def test_flush_drains_even_inside_a_backoff(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: got.extend(rows), WRITE_BEHIND_BACKOFF_S=30)
    for i in range(4):
        _submit_no_worker("k", {"i": i})
    wb.note_timeout()
    assert wb.flush(2.0) == 0 and len(got) == 4


def test_worker_thread_writes_in_the_background(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: got.extend(rows), WRITE_BEHIND_FLUSH_S=0.05)
    assert wb.submit("k", {"i": 1}) is True                 # real worker
    for _ in range(100):
        if got:
            break
        time.sleep(0.02)
    wb.stop()
    assert got == [{"i": 1}]


def test_env_values_that_are_blank_or_bad_fall_back(monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_MAX_QUEUE", "abc")
    monkeypatch.setenv("WRITE_BEHIND_BATCH", "")
    monkeypatch.setenv("WRITE_BEHIND_FLUSH_S", "-1")
    monkeypatch.setenv("WRITE_BEHIND_BACKOFF_S", "x")
    assert (wb.max_queue(), wb.batch_size(), wb.flush_s(), wb.backoff_s()) == (5000, 50, 1.0, 20.0)


# ── divert_pending_waits ───────────────────────────────────────────────────────────────────────────────────────────────
class Col:
    def __init__(self, key):
        self.key = key


def _decision(action="WAIT", divertable=True, id_=None, dtype="ENTRY"):
    cols = ["id", "mode", "symbol", "decision_type", "action", "reasoning", "created_at"]
    d = NS(id=id_, mode="REAL", symbol="AAA", decision_type=dtype, action=action, reasoning="r", created_at=None,
           _wb_divertable=divertable, __table__=NS(columns=[Col(c) for c in cols]))
    return d


class FakeDB:
    def __init__(self, new):
        self.new, self.expunged = list(new), []

    def expunge(self, o):
        self.expunged.append(o)


def test_divert_moves_only_flagged_unflushed_waits(monkeypatch):
    got = []
    _setup(monkeypatch, lambda db, rows: None)
    wb.register("entry_wait", lambda db, rows: got.extend(rows))
    keep_enter = _decision(action="ENTER")
    keep_unflagged = _decision(divertable=False)
    keep_flushed = _decision(id_=9)
    keep_exit = _decision(dtype="EXIT")
    move = _decision()
    db = FakeDB([keep_enter, keep_unflagged, keep_flushed, keep_exit, move])
    orig = wb._ensure_worker
    wb._ensure_worker = lambda: None
    try:
        assert wb.divert_pending_waits(db) == 1
    finally:
        wb._ensure_worker = orig
    assert db.expunged == [move]
    wb.drain_once()
    assert len(got) == 1 and got[0]["symbol"] == "AAA" and "id" not in got[0] and got[0]["created_at"] is not None


def test_divert_leaves_rows_in_the_session_when_the_queue_is_off(monkeypatch):
    _setup(monkeypatch, lambda db, rows: None)
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")
    db = FakeDB([_decision()])
    assert wb.divert_pending_waits(db) == 0 and db.expunged == []


def test_divert_never_raises_on_garbage(monkeypatch):
    _setup(monkeypatch, lambda db, rows: None)
    assert wb.divert_pending_waits(object()) == 0
