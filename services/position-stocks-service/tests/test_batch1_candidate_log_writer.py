"""Batch 1 (2026-11-02): candidate log writer - de-dup, queue, batch write, retention, rollback flag."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from orders import candidate_log_writer as clw


@pytest.fixture(autouse=True)
def _clean():
    clw._seen.clear()
    while not clw._q.empty():
        clw._q.get_nowait()
    for k in clw.stats:
        clw.stats[k] = 0
    yield


def _row(sym="AAA", reason="STOP_ATR_FLOOR:STOP_TOO_TIGHT:stop 1.20% < 0.30 x d"):
    return dict(symbol=sym, window_source="5m", pct_change=1.0, composite_score=None, decision="SKIPPED", reason=reason)


def test_same_symbol_and_reason_head_is_deduped_inside_window():
    assert clw.is_duplicate("AAA", "SKIPPED", "BOARD_CHASE:2.35 ATR past", now=100.0) is False
    # the number differs, the first two ':' parts match -> same key
    assert clw.is_duplicate("AAA", "SKIPPED", "BOARD_CHASE:2.37 ATR past", now=200.0) is True
    assert clw.stats["deduped"] == 1


def test_window_expiry_and_other_symbol_are_not_deduped():
    clw.is_duplicate("AAA", "SKIPPED", "RR_FLOOR: reward", now=0.0)
    assert clw.is_duplicate("AAA", "SKIPPED", "RR_FLOOR: reward", now=config.CANDIDATE_LOG_DEDUPE_S + 1) is False
    assert clw.is_duplicate("BBB", "SKIPPED", "RR_FLOOR: reward", now=1.0) is False


def test_different_gate_for_same_symbol_is_kept():
    clw.is_duplicate("AAA", "SKIPPED", "RR_FLOOR: reward", now=0.0)
    assert clw.is_duplicate("AAA", "SKIPPED", "VWAP_BELOW: x", now=1.0) is False


def test_dedupe_off_when_window_zero(monkeypatch):
    monkeypatch.setattr(config, "CANDIDATE_LOG_DEDUPE_S", 0.0)
    assert clw.is_duplicate("AAA", "SKIPPED", "X:Y", now=0.0) is False
    assert clw.is_duplicate("AAA", "SKIPPED", "X:Y", now=1.0) is False


def test_full_queue_returns_false_so_caller_can_write_old_way(monkeypatch):
    monkeypatch.setattr(clw, "_ensure_thread", lambda: None)
    monkeypatch.setattr(clw, "_q", __import__("queue").Queue(maxsize=1))
    assert clw.submit(_row("A1", "G1:a")) is True
    assert clw.submit(_row("A2", "G2:a")) is False
    assert clw.stats["dropped_full"] == 1


def _mem_factory(monkeypatch):
    import models
    eng = create_engine("sqlite://")
    models.ScalpCandidateLog.__table__.create(eng)
    factory = sessionmaker(bind=eng, autoflush=False)
    import db as _db
    monkeypatch.setattr(_db, "get_session_factory", lambda: factory)
    return factory


def test_write_batch_inserts_all_rows_in_one_commit(monkeypatch):
    import models
    factory = _mem_factory(monkeypatch)
    rows = [dict(_row(f"S{i}", f"G{i}:x"), created_at=datetime.utcnow()) for i in range(5)]
    assert clw.write_batch(rows) == 5
    s = factory()
    assert s.query(models.ScalpCandidateLog).count() == 5


def test_write_batch_failure_is_swallowed_and_counted(monkeypatch):
    import db as _db
    monkeypatch.setattr(_db, "get_session_factory", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert clw.write_batch([_row()]) == 0
    assert clw.stats["failed_batches"] == 1


def test_timeout_sets_backoff(monkeypatch):
    class Bad:
        def add_all(self, r): raise RuntimeError("DPY-4024: call timeout of 8000 ms exceeded")
        def rollback(self): pass
        def close(self): pass
    import db as _db
    monkeypatch.setattr(_db, "get_session_factory", lambda: (lambda: Bad()))
    monkeypatch.setattr(clw, "_backoff_until", 0.0)
    assert clw.write_batch([_row()]) == 0
    assert clw._backoff_until > 0


def test_purge_deletes_only_old_rows_in_chunks(monkeypatch):
    import models
    factory = _mem_factory(monkeypatch)
    s = factory()
    now = datetime.utcnow()
    for i in range(12):
        s.add(models.ScalpCandidateLog(symbol=f"O{i}", window_source="5m", pct_change=1.0, decision="SKIPPED", reason="x",
                                       created_at=now - timedelta(days=40)))
    for i in range(3):
        s.add(models.ScalpCandidateLog(symbol=f"N{i}", window_source="5m", pct_change=1.0, decision="SKIPPED", reason="x", created_at=now))
    s.commit()
    assert clw.purge_old(s, now=now, chunk=5) == 12
    assert s.query(models.ScalpCandidateLog).count() == 3


def test_purge_zero_days_keeps_everything(monkeypatch):
    monkeypatch.setattr(config, "CANDIDATE_LOG_RETENTION_DAYS", 0.0)
    assert clw.purge_old(object()) == 0


def test_entry_log_candidate_uses_writer_for_skipped_and_stays_sync_for_entered(monkeypatch):
    from orders import entry
    monkeypatch.setattr(config, "CANDIDATE_LOG_ASYNC", True)
    calls = []
    monkeypatch.setattr(clw, "submit", lambda row: calls.append(row) or True)

    class DB:
        def __init__(self): self.added, self.commits = [], 0
        def add(self, o): self.added.append(o)
        def commit(self): self.commits += 1
        def rollback(self): pass

    class Cand:
        symbol, window_label, pct_change, composite_score = "ZZZ", "5m", 1.2, 0.5

    db = DB()
    entry._log_candidate(db, Cand(), "SKIPPED", "RR_FLOOR: reward")
    assert len(calls) == 1 and db.added == [] and db.commits == 1     # queued; commit kept for pending state
    entry._log_candidate(db, Cand(), "ENTERED", "ok")
    assert len(db.added) == 1                                          # ENTERED stays synchronous


def test_async_is_the_shipped_default():
    import importlib
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.py")).read()
    assert '_get_bool("CANDIDATE_LOG_ASYNC", True)' in src


def test_entry_log_candidate_old_path_when_flag_off(monkeypatch):
    from orders import entry
    monkeypatch.setattr(config, "CANDIDATE_LOG_ASYNC", False)

    class DB:
        def __init__(self): self.added = []
        def add(self, o): self.added.append(o)
        def commit(self): pass
        def rollback(self): pass

    class Cand:
        symbol, window_label, pct_change, composite_score = "ZZZ", "5m", 1.2, 0.5

    db = DB()
    entry._log_candidate(db, Cand(), "SKIPPED", "RR_FLOOR: reward")
    assert len(db.added) == 1
