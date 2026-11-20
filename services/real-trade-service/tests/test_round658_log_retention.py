"""Round 658: nightly retention of old WAIT decisions and HOLD exit rows - window, safety floor, batching, error handling, wiring, indexes.

The batching tests run prune_once against a tiny fake session (a stub `sqlalchemy.select` is installed when the real one is missing), so they
need no database; the last test runs the real queries on in-memory SQLite when SQLAlchemy is installed."""
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import log_retention as lr

IST = lr._IST
HERE = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LOG_RETENTION_ENABLED", "LOG_RETENTION_DAYS", "LOG_RETENTION_BATCH", "LOG_RETENTION_MAX_BATCHES", "LOG_RETENTION_PAUSE_S",
              "LOG_RETENTION_START_IST", "LOG_RETENTION_END_IST"):
        monkeypatch.delenv(k, raising=False)
    lr.reset_for_tests()
    yield
    lr.reset_for_tests()


def ist(h, m=0, day=20):
    return datetime(2026, 11, day, h, m, tzinfo=IST)


# ── window ───────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("h,m,expected", [(15, 44, False), (15, 45, True), (23, 59, True), (0, 0, True), (8, 29, True), (8, 30, False),
                                          (9, 15, False), (12, 0, False), (15, 30, False)])
def test_window_never_covers_market_hours(h, m, expected):
    assert lr.in_window(ist(h, m)) is expected


def test_window_day_keeps_the_evening_together():
    assert lr.window_day(ist(20, 0, day=20)) == "2026-11-20"
    assert lr.window_day(ist(2, 0, day=21)) == "2026-11-20"     # after midnight still belongs to the 20th
    assert lr.window_day(ist(8, 30, day=21)) == "2026-11-21"


def test_bad_window_env_falls_back(monkeypatch):
    monkeypatch.setenv("LOG_RETENTION_START_IST", "banana")
    monkeypatch.setenv("LOG_RETENTION_END_IST", "25:99")
    assert lr.in_window(ist(16, 0)) and not lr.in_window(ist(10, 0))


# ── safety floors ────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [("30", 30), ("7", 7), ("6", 30), ("0", 30), ("-5", 30), ("abc", 30), ("90", 90)])
def test_keep_days_below_seven_is_ignored(monkeypatch, raw, expected):
    monkeypatch.setenv("LOG_RETENTION_DAYS", raw)
    assert lr.keep_days() == expected


def test_batch_is_capped_for_oracle_in_list(monkeypatch):
    monkeypatch.setenv("LOG_RETENTION_BATCH", "5000")
    assert lr.batch_size() == 1000
    monkeypatch.setenv("LOG_RETENTION_BATCH", "10")
    assert lr.batch_size() == 500


def test_cutoff_is_naive_utc_and_days_back():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    c = lr.cutoff_utc(now, 30)
    assert c.tzinfo is None and c == datetime(2026, 10, 21, 12, 0)


def test_disabled_flag(monkeypatch):
    monkeypatch.setenv("LOG_RETENTION_ENABLED", "0")
    assert lr.enabled() is False and lr.start() is False
    assert lr.maybe_run(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)) is None


# ── prune_once loop (fake session) ───────────────────────────────────────────────────────────────────────────────────
class _Col:
    def __getattr__(self, name):
        return self

    def __call__(self, *a, **k):
        return self

    def __eq__(self, o):
        return self

    def __lt__(self, o):
        return self

    def __invert__(self):
        return self

    __hash__ = object.__hash__


class _Model:
    id = _Col()
    action = _Col()
    created_at = _Col()
    evaluated_at = _Col()
    decision_id = _Col()


class FakeDB:
    """Hands out `pending[label]` ids in chunks; delete() removes the chunk. Query label is decided by the model passed to query()."""

    def __init__(self, decisions, holds, fail_on_commit=None, exc=None):
        self.left = {"D": list(decisions), "E": list(holds)}
        self.commits, self.rollbacks, self.deleted = 0, 0, {"D": 0, "E": 0}
        self.fail_on_commit, self.exc = fail_on_commit, exc
        self._which = None

    def query(self, col_or_model):
        self._which = "D" if (col_or_model is _D.id or col_or_model is _D) else "E"
        return self

    def filter(self, *a):
        return self

    def limit(self, n):
        self._n = n
        return self

    def all(self):
        return [(i,) for i in self.left[self._which][: self._n]]

    def delete(self, synchronize_session=None):
        pass

    def commit(self):
        self.commits += 1
        if self.fail_on_commit and self.commits == self.fail_on_commit:
            raise self.exc

    def rollback(self):
        self.rollbacks += 1


_D, _O, _E = type("D", (_Model,), {"id": _Col()}), _Model, type("E", (_Model,), {"id": _Col()})


class _FakeModels:
    TradeDecision, TradeOrder, TradeExitDecision = _D, _O, _E


@pytest.fixture
def fake_sqlalchemy(monkeypatch):
    m = types.ModuleType("sqlalchemy")
    m.select = lambda *a, **k: _Col()
    monkeypatch.setitem(sys.modules, "sqlalchemy", m)


def _run(db, **kw):
    # make delete() actually consume the ids the last all() returned
    orig_all = db.all

    def all_():
        rows = orig_all()
        db._last = (db._which, [r[0] for r in rows])
        return rows
    db.all = all_
    orig_delete = db.delete

    def delete_(synchronize_session=None):
        w, ids = db._last
        db.left[w] = db.left[w][len(ids):]
        db.deleted[w] += len(ids)
    db.delete = delete_
    return lr.prune_once(db, _FakeModels, datetime(2026, 10, 1), sleep=lambda s: None, **kw)


def test_prune_deletes_in_batches_and_finishes(fake_sqlalchemy):
    db = FakeDB(range(1, 1201), range(1, 701))
    out = _run(db, batch=500, budget=100, pause=0)
    assert out["complete"] and out["error"] is None and out["stopped"] is None
    assert (out["decisions"], out["exit_holds"]) == (1200, 700)
    assert db.commits == out["batches"] == 3 + 2          # 500+500+200, 500+200
    assert db.left == {"D": [], "E": []}


def test_prune_with_nothing_to_do_still_completes(fake_sqlalchemy):
    out = _run(FakeDB([], []), batch=500, budget=10, pause=0)
    assert out["complete"] and out["batches"] == 0


def test_budget_stops_the_run_and_leaves_the_rest(fake_sqlalchemy):
    db = FakeDB(range(1, 5001), range(1, 101))
    out = _run(db, batch=500, budget=4, pause=0)
    assert out["stopped"] == "budget" and not out["complete"] and out["batches"] == 4
    assert len(db.left["D"]) == 3000 and len(db.left["E"]) == 100


def test_window_closing_stops_between_batches(fake_sqlalchemy):
    db = FakeDB(range(1, 5001), [])
    calls = {"n": 0}

    def keep_going():
        calls["n"] += 1
        return calls["n"] <= 2
    out = _run(db, batch=500, budget=100, pause=0, keep_going=keep_going)
    assert out["stopped"] == "window" and out["batches"] == 2 and not out["complete"]


def test_db_error_rolls_back_stops_and_does_not_raise(fake_sqlalchemy):
    db = FakeDB(range(1, 2001), range(1, 11), fail_on_commit=2, exc=RuntimeError("ORA-00060 deadlock"))
    out = _run(db, batch=500, budget=100, pause=0)
    assert out["error"].startswith("RuntimeError") and not out["complete"]
    assert db.rollbacks == 1 and out["batches"] == 1


def test_call_timeout_opens_write_behind_backoff(fake_sqlalchemy, monkeypatch):
    seen = []
    wb = types.ModuleType("write_behind")
    wb.note_timeout = lambda: seen.append(1)
    monkeypatch.setitem(sys.modules, "write_behind", wb)
    db = FakeDB(range(1, 600), [], fail_on_commit=1, exc=RuntimeError("DPY-4024: call timeout of 8000 ms exceeded"))
    out = _run(db, batch=500, budget=10, pause=0)
    assert out["error"] and seen == [1]


# ── scheduler ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _utc(h, m=0):
    return datetime(2026, 11, 20, h, m, tzinfo=IST).astimezone(timezone.utc)


def _stub_prune(monkeypatch, result):
    calls = []

    def fake(db, models, cutoff, **kw):
        calls.append(cutoff)
        return dict(result)
    monkeypatch.setattr(lr, "prune_once", fake)
    return calls


class _S:
    closed = False

    def close(self):
        _S.closed = True


def test_does_not_run_in_market_hours(monkeypatch):
    calls = _stub_prune(monkeypatch, {"complete": True, "error": None})
    assert lr.maybe_run(_utc(11), session_factory=_S, models_mod=object()) is None and calls == []


def test_runs_once_per_evening_when_complete(monkeypatch):
    calls = _stub_prune(monkeypatch, {"complete": True, "error": None, "decisions": 3, "exit_holds": 4, "batches": 2})
    assert lr.maybe_run(_utc(16), session_factory=_S, models_mod=object())["complete"]
    assert lr.maybe_run(_utc(17), session_factory=_S, models_mod=object()) is None      # same evening
    assert len(calls) == 1 and _S.closed
    nxt = datetime(2026, 11, 21, 16, 0, tzinfo=IST).astimezone(timezone.utc)
    assert lr.maybe_run(nxt, session_factory=_S, models_mod=object()) is not None       # next evening runs again


def test_budget_stop_is_picked_up_on_the_next_check(monkeypatch):
    calls = _stub_prune(monkeypatch, {"complete": False, "stopped": "budget", "error": None, "decisions": 0, "exit_holds": 0, "batches": 400})
    lr.maybe_run(_utc(16), session_factory=_S, models_mod=object())
    lr.maybe_run(_utc(16, 5), session_factory=_S, models_mod=object())
    assert len(calls) == 2


def test_error_backs_off_for_half_an_hour(monkeypatch):
    calls = _stub_prune(monkeypatch, {"complete": False, "error": "RuntimeError: x", "decisions": 0, "exit_holds": 0, "batches": 0})
    lr.maybe_run(_utc(16), session_factory=_S, models_mod=object())
    assert lr.maybe_run(_utc(16, 5), session_factory=_S, models_mod=object()) is None
    assert len(calls) == 1 and lr.last_result()["error"]


def test_session_factory_failure_never_raises():
    def boom():
        raise RuntimeError("no db")
    res = lr.maybe_run(_utc(16), session_factory=boom, models_mod=object())
    assert res["error"].startswith("RuntimeError")


# ── wiring and indexes (source level) ────────────────────────────────────────────────────────────────────────────────
def test_started_and_stopped_from_main():
    src = (HERE / "main.py").read_text(encoding="utf-8")
    assert "import log_retention as _log_retention" in src and "_log_retention.start()" in src
    assert "log_retention.stop()" in src


def test_indexes_are_in_models_db_and_in_step():
    models = (HERE / "models.py").read_text(encoding="utf-8")
    dbsrc = (HERE / "db.py").read_text(encoding="utf-8")
    for name in ("ix_trade_decisions_mode_type_created", "ix_trade_decisions_mode_symbol_created", "ix_trade_exit_decisions_action_evaluated"):
        assert name in models and name in dbsrc
    # the retention queries filter on exactly the columns the exit index covers
    assert re.search(r'Index\("ix_trade_exit_decisions_action_evaluated", "action", "evaluated_at"\)', models)


def test_only_wait_and_hold_rows_are_ever_deleted():
    src = (HERE / "log_retention.py").read_text(encoding="utf-8")
    assert src.count('"WAIT"') == 1 and src.count('"HOLD"') == 1
    assert "decision_id" in src and "TRAIL_STOP" not in src.split('"""', 2)[2]      # exits other than HOLD are never selected in code


# ── real queries on SQLite (only where SQLAlchemy is installed) ──────────────────────────────────────────────────────
def test_real_queries_keep_enter_referenced_and_exit_rows():
    sa = pytest.importorskip("sqlalchemy")
    import models
    from sqlalchemy.orm import sessionmaker
    eng = sa.create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    old, new = datetime(2026, 9, 1), datetime(2026, 11, 19)

    def dec(action, when):
        r = models.TradeDecision(mode="REAL", symbol="ABC", decision_type="ENTRY", action=action, created_at=when)
        s.add(r)
        s.flush()
        return r.id
    old_wait, old_wait_ref, old_enter, new_wait = dec("WAIT", old), dec("WAIT", old), dec("ENTER", old), dec("WAIT", new)
    s.add(models.TradeOrder(mode="REAL", symbol="ABC", side="BUY", qty=1, decision_id=old_wait_ref))
    s.flush()
    pos = models.TradePosition(mode="REAL", symbol="ABC", status="CLOSED", avg_entry_price=100.0)
    s.add(pos)
    s.flush()
    for act, when in (("HOLD", old), ("HOLD", new), ("TRAIL_STOP", old), ("FULL_EXIT", old)):
        s.add(models.TradeExitDecision(position_id=pos.id, action=act, evaluated_at=when))
    s.commit()

    out = lr.prune_once(s, models, datetime(2026, 10, 1), batch=50, budget=10, pause=0, sleep=lambda x: None)
    assert out["complete"] and out["error"] is None and (out["decisions"], out["exit_holds"]) == (1, 1)
    assert {r[0] for r in s.query(models.TradeDecision.id)} == {old_wait_ref, old_enter, new_wait}
    assert sorted(r[0] for r in s.query(models.TradeExitDecision.action)) == ["FULL_EXIT", "HOLD", "TRAIL_STOP"]
