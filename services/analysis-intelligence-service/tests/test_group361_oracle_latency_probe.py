"""group361: oracle_compat latency probe + keep-warm, run against a REAL sqlite engine (no Oracle needed)."""
import importlib.util
import logging
import os
import time

import pytest
from sqlalchemy import create_engine, text

_PATH = os.path.join(os.path.dirname(__file__), "..", "fundamental/oracle_compat.py")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ORACLE_SLOW_QUERY_MS", "ORACLE_KEEPWARM_S", "ORACLE_LATENCY_PROBE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ORACLE_KEEPWARM_S", "0")  # no background thread unless a test asks


@pytest.fixture
def m():
    spec = importlib.util.spec_from_file_location("oc_probe_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mod._WARM_STOP.set()


def _engine(tmp_path):
    return create_engine("sqlite:///%s" % (tmp_path / "t.db"), pool_size=2, max_overflow=0)


def test_probe_counts_executes_and_connects(m, tmp_path):
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
        c.execute(text("SELECT 2"))
    snap = m.latency_snapshot()
    (row,) = snap["engines"].values()
    assert row["exec"]["n"] == 2 and row["connect"]["n"] == 1
    assert row["pool"]["checked_out"] == 0


def test_slow_statement_is_logged_and_recorded(m, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("ORACLE_SLOW_QUERY_MS", "0.0001")
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    with caplog.at_level(logging.WARNING, logger="oracle-compat"):
        with eng.connect() as c:
            c.execute(text("SELECT   1"))
    (row,) = m.latency_snapshot()["engines"].values()
    assert row["exec"]["slow_n"] >= 1 and row["last_slow"]["what"] == "SELECT 1"
    assert any("oracle slow" in r.message for r in caplog.records)


def test_slow_log_is_throttled(m, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("ORACLE_SLOW_QUERY_MS", "0.0001")
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    with caplog.at_level(logging.WARNING, logger="oracle-compat"):
        with eng.connect() as c:
            for _ in range(20):
                c.execute(text("SELECT 1"))
    assert sum("oracle slow exec" in r.message for r in caplog.records) == 1
    (row,) = m.latency_snapshot()["engines"].values()
    assert row["exec"]["slow_n"] == 20  # all counted, one logged


def test_failed_statement_does_not_skew_next_timing(m, tmp_path):
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    with eng.connect() as c:
        with pytest.raises(Exception):
            c.execute(text("SELECT * FROM no_such_table"))
        c.rollback()
        c.execute(text("SELECT 1"))
    (row,) = m.latency_snapshot()["engines"].values()
    assert row["exec"]["n"] == 1 and row["exec"]["max_ms"] < 1000


def test_probe_switch_off(m, tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_LATENCY_PROBE", "0")
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
    assert m.latency_snapshot()["engines"] == {}


def test_bad_env_falls_back_to_defaults(m, monkeypatch):
    monkeypatch.setenv("ORACLE_SLOW_QUERY_MS", "fast")
    monkeypatch.setenv("ORACLE_KEEPWARM_S", "-5")
    assert m._slow_ms() == 1500.0 and m._keepwarm_s() == 0.0


def test_keepwarm_touches_only_idle_connections(m, tmp_path):
    eng = _engine(tmp_path)
    m._attach_latency_probe(eng)
    m._WARM.add(eng)
    assert m._keepwarm_tick() == 0               # nothing idle yet: must NOT open a session
    with eng.connect() as c:
        c.execute(text("SELECT 1"))               # now one idle connection sits in the pool
    assert eng.pool.checkedin() == 1
    assert m._keepwarm_tick() == 1
    assert eng.pool.checkedin() == 1 and eng.pool.checkedout() == 0
    assert m.latency_snapshot()["keepwarm"]["pings"] == 1


def test_keepwarm_failure_is_counted_not_raised(m, tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
    m._WARM.add(eng)
    monkeypatch.setattr(m, "_ping", lambda e: (_ for _ in ()).throw(RuntimeError("net down")))
    assert m._keepwarm_tick() == 0
    kw = m.latency_snapshot()["keepwarm"]
    assert kw["failures"] == 1 and "net down" in kw["last_failure"]


def test_register_starts_one_daemon_thread_and_off_switch(m, tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_KEEPWARM_S", "60")
    e1, e2 = _engine(tmp_path), create_engine("sqlite:///%s" % (tmp_path / "u.db"))
    m._register_keepwarm(e1)
    m._register_keepwarm(e2)
    th = m._WARM_STATE["thread"]
    assert th.is_alive() and th.daemon and th.name == "oracle-keepwarm"
    assert m.latency_snapshot()["keepwarm"]["engines"] == 2
    m._WARM_STOP.set()
    th.join(timeout=3)
    assert not th.is_alive()
    monkeypatch.setenv("ORACLE_KEEPWARM_S", "0")
    m._WARM.clear()
    m._register_keepwarm(e1)
    assert len(m._WARM) == 0


def test_unweakrefable_stub_engine_is_skipped_silently(m, monkeypatch):
    monkeypatch.setenv("ORACLE_KEEPWARM_S", "60")
    m._attach_latency_probe(object())  # must not raise


def test_pct_of_empty_is_zero(m):
    assert m._pct([], 0.95) == 0.0 and m._pct([5.0], 0.95) == 5.0


def test_tick_stops_when_stop_event_set(m, tmp_path):
    eng = _engine(tmp_path)
    with eng.connect() as c:
        c.execute(text("SELECT 1"))
    m._WARM.add(eng)
    m._WARM_STOP.set()
    assert m._keepwarm_tick() == 0


def test_loop_runs_ticks_and_skips_when_switched_off(m, monkeypatch):
    calls = []

    class Stop:
        def __init__(self):
            self.n = 0

        def wait(self, t):
            self.n += 1
            return self.n > 3          # three iterations, then stop

        def is_set(self):
            return False

        def set(self):
            pass

        def clear(self):
            pass

    monkeypatch.setattr(m, "_WARM_STOP", Stop())
    monkeypatch.setattr(m, "_keepwarm_tick", lambda: calls.append(1) or 0)
    seq = iter([60.0, 60.0, 0.0, 0.0, 60.0, 60.0, 60.0])
    monkeypatch.setattr(m, "_keepwarm_s", lambda: next(seq))
    m._keepwarm_loop()
    assert len(calls) == 2   # the middle iteration (keep-warm switched off) made no tick
