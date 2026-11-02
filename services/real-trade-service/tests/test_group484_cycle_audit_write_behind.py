"""E285 round 131: the per-cycle summary audit rows (ENTRY_CYCLE, ORDERS_EXPIRED, EXIT_ORDERS_EXPIRED) and the adaptive-metric history rows go behind the cycle.
2 Nov VM log: single INSERTs into trade_audit_log took 2.3-4.6 s inside the entry / exit tick. Pure: stand-in `models`, `sqlalchemy`, fake sessions, no database."""
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import write_behind as wb

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def logger_mod(monkeypatch):
    fake_models = types.ModuleType("models")

    class TradeAuditLog:                                   # noqa: N801
        def __init__(self, **kw):
            self.__dict__.update(kw)
    fake_models.TradeAuditLog = TradeAuditLog
    sa = types.ModuleType("sqlalchemy")
    orm = types.ModuleType("sqlalchemy.orm")
    orm.Session = object
    for name, mod in (("models", fake_models), ("sqlalchemy", sa), ("sqlalchemy.orm", orm)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("audit_logger_under_test", ROOT / "audit" / "logger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DB:
    def __init__(self):
        self.added, self.commits, self.rollbacks = [], 0, 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "1")
    wb.reset_for_tests()
    wb.stop()
    orig = wb._ensure_worker
    wb._ensure_worker = lambda: None                       # no daemon thread: rows stay queued for the assertions
    yield wb
    wb._ensure_worker = orig
    wb.reset_for_tests()


def _cfg(monkeypatch, value):
    import config
    monkeypatch.setattr(config, "CYCLE_AUDIT_WRITE_BEHIND", value, raising=False)


def test_row_is_queued_and_the_cycle_session_is_not_touched(logger_mod, queue, monkeypatch):
    _cfg(monkeypatch, True)
    db = _DB()
    logger_mod.log_action_behind(db, actor="system", action="ENTRY_CYCLE", mode="REAL", detail="evaluated=3")
    assert db.commits == 0 and db.added == []
    assert wb.status()["queued"] == 1
    kind, payload, _ = wb._q[0]
    assert kind == "audit" and payload == {"mode": "REAL", "actor": "system", "action": "ENTRY_CYCLE", "detail": "evaluated=3"}


def test_switch_off_writes_synchronously(logger_mod, queue, monkeypatch):
    _cfg(monkeypatch, False)
    db = _DB()
    logger_mod.log_action_behind(db, actor="system", action="ORDERS_EXPIRED", mode="REAL", detail="count=2")
    assert db.commits == 1 and len(db.added) == 1 and db.added[0].action == "ORDERS_EXPIRED"
    assert wb.status()["queued"] == 0


def test_queue_disabled_falls_back_to_synchronous(logger_mod, monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")
    _cfg(monkeypatch, True)
    db = _DB()
    logger_mod.log_action_behind(db, actor="system", action="EXIT_ORDERS_EXPIRED", mode="REAL", detail="count=1")
    assert db.commits == 1 and len(db.added) == 1


def test_submit_raising_falls_back_and_never_raises(logger_mod, monkeypatch):
    _cfg(monkeypatch, True)
    monkeypatch.setattr(wb, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    db = _DB()
    logger_mod.log_action_behind(db, actor="system", action="ENTRY_CYCLE", mode="REAL", detail="x")
    assert db.commits == 1


def test_sync_log_action_is_unchanged(logger_mod, queue):
    db = _DB()
    logger_mod.log_action(db, actor="admin", action="ARMED", mode="REAL", detail="")
    assert db.commits == 1 and wb.status()["queued"] == 0


def test_metric_and_audit_kinds_are_registered():
    assert "metric" in wb._handlers and "audit" in wb._handlers


def test_metric_handler_inserts_then_prunes_each_metric_once(monkeypatch):
    calls = []

    class Snap:
        class _Col:
            def __eq__(self, o):
                return ("eq", o)

            def __lt__(self, o):
                return ("lt", o)
        metric_name = _Col()
        recorded_at = _Col()

    fake_models = types.ModuleType("models")
    fake_models.AdaptiveMetricSnapshot = Snap
    amp = types.ModuleType("adaptive_market_params")
    amp.ADAPTIVE_HISTORY_DAYS = 30
    monkeypatch.setitem(sys.modules, "models", fake_models)
    monkeypatch.setitem(sys.modules, "adaptive_market_params", amp)

    class Q:
        def filter(self, *a):
            calls.append(("filter", len(a)))
            return self

        def delete(self, synchronize_session=False):
            calls.append(("delete", synchronize_session))

    class S:
        def bulk_insert_mappings(self, model, rows):
            calls.append(("bulk", model, rows))

        def commit(self):
            calls.append(("commit",))

        def rollback(self):
            calls.append(("rollback",))

        def query(self, m):
            return Q()

    now = datetime.now(timezone.utc)
    wb._write_metric(S(), [
        {"metric_name": "a", "value": 1.0, "recorded_at": now},
        {"metric_name": "a", "value": 2.0, "recorded_at": None},
        {"metric_name": "b", "value": 3.0},
    ])
    bulk = [c for c in calls if c[0] == "bulk"][0]
    assert bulk[1] is Snap and len(bulk[2]) == 3
    assert "recorded_at" in bulk[2][0] and "recorded_at" not in bulk[2][1] and "recorded_at" not in bulk[2][2]
    assert [c for c in calls if c[0] == "delete"].__len__() == 2          # one prune per distinct metric
    assert calls.index(("commit",)) < calls.index(("delete", False))        # readings are committed before the prune


def test_metric_prune_failure_keeps_readings_and_rolls_back(monkeypatch):
    calls = []

    fake_models = types.ModuleType("models")
    fake_models.AdaptiveMetricSnapshot = type("Snap2", (), {"metric_name": 1, "recorded_at": 2})
    amp = types.ModuleType("adaptive_market_params")
    amp.ADAPTIVE_HISTORY_DAYS = 30
    monkeypatch.setitem(sys.modules, "models", fake_models)
    monkeypatch.setitem(sys.modules, "adaptive_market_params", amp)

    class S:
        def bulk_insert_mappings(self, model, rows):
            calls.append("bulk")

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def query(self, m):
            raise RuntimeError("prune broke")

    wb._write_metric(S(), [{"metric_name": "a", "value": 1.0}])           # must not raise
    assert calls == ["bulk", "commit", "rollback"]


def test_config_switch_exists_and_defaults_on():
    import config
    assert getattr(config, "CYCLE_AUDIT_WRITE_BEHIND", None) is True


def test_call_sites_use_the_behind_helper():
    entry = (ROOT / "entry_engine" / "entry.py").read_text()
    exit_ = (ROOT / "exit_engine" / "exit.py").read_text()
    assert 'log_action_behind(db, actor="system", action="ENTRY_CYCLE"' in entry
    assert 'log_action_behind(db, actor="system", action="ORDERS_EXPIRED"' in entry
    assert 'log_action_behind(db, actor="system", action="EXIT_ORDERS_EXPIRED"' in exit_
    assert 'write_behind.submit("metric"' in (ROOT / "adaptive_market_params.py").read_text()
