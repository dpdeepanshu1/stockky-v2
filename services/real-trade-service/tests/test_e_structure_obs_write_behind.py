"""E-structure-obs (29 Oct 2026): the structure_observations INSERT timed out (DPY-4024) inside the entry cycle's transaction, and the EXIT_CYCLE
audit commit stalled the exit tick. Both now go through write_behind; the hourly reconcile names a CDSL eDIS/TPIN block as the cause of a stale SELL.

    cd services/real-trade-service
    python -m pytest tests/test_e_structure_obs_write_behind.py -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import write_behind as wb
from entry_engine import structure_observation as so
from execution import hourly_reconcile as hr

SUMMARY = {"ms_quality": "ok", "ms_stop": 95.0, "ms_target": 110.0, "ms_atr": 2.0}
VERDICT = {"opinion": "ok", "would_reject": False, "reasons": []}
NOW = datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc)


class Db:
    """A session that must NOT be touched when the row is queued."""
    def __init__(self):
        self.added, self.queries = [], 0

    def add(self, r):
        self.added.append(r)

    def query(self, *a, **k):
        self.queries += 1
        raise AssertionError("the entry cycle must not read or write the DB for an observation when write-behind is on")


def _wb_on(monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "1")
    wb.reset_for_tests()
    wb.stop()
    monkeypatch.setattr(wb, "_ensure_worker", lambda: None)      # no daemon thread: tests drain by hand
    so.reset()


def test_record_queues_and_never_touches_the_cycle_session(monkeypatch):
    _wb_on(monkeypatch)
    monkeypatch.setattr(config, "STRUCTURE_OBS_WRITE_BEHIND", True, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_OBS_ENABLED", True, raising=False)
    db = Db()
    assert so.record(db, "ARVIND", "REAL", 100.0, VERDICT, SUMMARY, "enforce", False, now_ts=1_700_000_000.0) is True
    assert db.added == [] and db.queries == 0
    assert wb.status()["queued"] == 1 and so.stats()["queued"] == 1
    # same symbol / mode / day again: dropped from memory, nothing more queued
    assert so.record(db, "ARVIND", "REAL", 101.0, VERDICT, SUMMARY, "enforce", False, now_ts=1_700_000_100.0) is False
    assert wb.status()["queued"] == 1


def test_switch_off_or_queue_disabled_keeps_the_old_in_session_write(monkeypatch):
    _wb_on(monkeypatch)
    monkeypatch.setattr(config, "STRUCTURE_OBS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_OBS_WRITE_BEHIND", False, raising=False)
    added = []
    with mock.patch.object(so, "_model", lambda: (lambda **kw: NS(**kw))), mock.patch.object(so, "_exists", lambda *a: False):
        db = NS(add=added.append)
        assert so.record(db, "ARVIND", "REAL", 100.0, VERDICT, SUMMARY, "enforce", False, now_ts=1_700_000_000.0) is True
    assert len(added) == 1 and wb.status()["queued"] == 0
    # switch ON but WRITE_BEHIND_ENABLED=0 -> submit() refuses -> same fallback
    so.reset()
    monkeypatch.setattr(config, "STRUCTURE_OBS_WRITE_BEHIND", True, raising=False)
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")
    with mock.patch.object(so, "_model", lambda: (lambda **kw: NS(**kw))), mock.patch.object(so, "_exists", lambda *a: False):
        assert so.record(NS(add=added.append), "LT", "REAL", 100.0, VERDICT, SUMMARY, "enforce", False, now_ts=1_700_000_000.0) is True
    assert len(added) == 2


def test_worker_handler_skips_rows_already_stored_and_duplicates_in_the_batch(monkeypatch):
    inserted = []
    fake_models = NS(StructureObservation=object())
    monkeypatch.setitem(sys.modules, "models", fake_models)
    db = NS(bulk_insert_mappings=lambda model, rows: inserted.extend(rows))
    rows = [{"mode": "REAL", "symbol": "A", "obs_day": "2026-10-29"}, {"mode": "REAL", "symbol": "A", "obs_day": "2026-10-29"},
            {"mode": "REAL", "symbol": "B", "obs_day": "2026-10-29"}, {"mode": "real", "symbol": "C", "obs_day": "2026-10-29"}]
    with mock.patch.object(so, "_exists", lambda d, m, s, day: s == "B"):
        wb._write_structure_obs(db, rows)
    assert sorted(r["symbol"] for r in inserted) == ["A", "C"]
    assert all(r.get("created_at") for r in inserted)


def test_a_timed_out_observation_batch_waits_out_the_backoff_instead_of_failing_a_cycle(monkeypatch):
    _wb_on(monkeypatch)

    def boom(db, rows):
        raise RuntimeError("(oracledb.exceptions.DatabaseError) DPY-4024: call timeout of 8000 ms exceeded")
    wb.register("structure_obs", boom)
    wb.configure(lambda: NS(commit=lambda: None, rollback=lambda: None, close=lambda: None))
    assert wb.submit("structure_obs", {"mode": "REAL", "symbol": "A", "obs_day": "d"}) is True
    assert wb.drain_once() == 0
    st = wb.status()
    assert st["queued"] == 1 and st["dropped_failed"] == 0 and st["backoff_remaining_s"] > 0     # kept, not dropped


def test_audit_kind_is_registered_and_writes_trade_audit_rows(monkeypatch):
    _wb_on(monkeypatch)
    inserted = []
    fake_models = NS(TradeAuditLog="TAL")
    monkeypatch.setitem(sys.modules, "models", fake_models)
    wb.configure(lambda: NS(bulk_insert_mappings=lambda m, rows: inserted.append((m, list(rows))), commit=lambda: None,
                            rollback=lambda: None, close=lambda: None))
    assert wb.submit("audit", {"mode": "REAL", "actor": "system", "action": "EXIT_CYCLE", "detail": "{}"}) is True
    assert wb.drain_once() == 1
    assert inserted == [("TAL", [{"mode": "REAL", "actor": "system", "action": "EXIT_CYCLE", "detail": "{}"}])]


def test_exit_cycle_summary_row_is_wired_through_write_behind():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exit_engine", "exit.py"), encoding="utf-8").read()
    assert '_wb_audit.submit("audit"' in src and "if not _audit_queued:" in src


def test_flag_is_in_config_and_the_calibration_catalog():
    assert hasattr(config, "STRUCTURE_OBS_WRITE_BEHIND")
    from control import catalog
    names = {getattr(f, "name", None) or getattr(f, "key", None) for f in getattr(catalog, "FLAGS", [])}
    assert "STRUCTURE_OBS_WRITE_BEHIND" in names or "STRUCTURE_OBS_WRITE_BEHIND" in open(catalog.__file__, encoding="utf-8").read()


# ── hourly reconcile: eDIS cause ──────────────────────────────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.rows


def _edis_db(position_id=5):
    return NS(query=lambda model: _Q([NS(id=position_id)]))


def test_edis_marker_marks_the_stale_sell_with_its_cause(monkeypatch):
    monkeypatch.setitem(sys.modules, "models", NS(TradePosition=NS(mode=1, symbol=1, status=NS(in_=lambda x: 1))))
    marker = {"at": (NOW - timedelta(minutes=30)).isoformat()}
    with mock.patch("resilience.local_cache.load_snapshot", lambda db, key: marker if key == "cdsl_alert_last_5" else None):
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW) is True
    old = {"at": (NOW - timedelta(hours=9)).isoformat()}
    with mock.patch("resilience.local_cache.load_snapshot", lambda db, key: old):
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW) is False
    with mock.patch("resilience.local_cache.load_snapshot", lambda db, key: None):
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW) is False


def test_a_verified_edis_status_overrides_an_old_cdsl_marker(monkeypatch):
    """The marker outlives the fix by up to 6 h; once Dhan reports the holdings verified, the TPIN must not be blamed for a stale SELL."""
    monkeypatch.setitem(sys.modules, "models", NS(TradePosition=NS(mode=1, symbol=1, status=NS(in_=lambda x: 1))))
    marker = {"at": (NOW - timedelta(minutes=30)).isoformat()}
    with mock.patch("resilience.local_cache.load_snapshot", lambda db, key: marker):
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW, verified=True) is False
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW, verified=False) is True
        assert hr.edis_blocked(_edis_db(), "BANKINDIA", NOW, verified=None) is True      # status unreadable: keep the marker's word


def test_alert_text_names_the_tpin_step_only_when_the_cause_is_known():
    base = {"id": 1142, "symbol": "BANKINDIA", "side": "SELL", "status": "PLACED", "age_min": 103}
    plain = hr._format_alert(hr.compute_drift([], {}, [base]))
    assert "TPIN" not in plain and "#1142 BANKINDIA SELL still PLACED after 103 min" in plain
    d = hr.compute_drift([], {}, [dict(base, cause=hr.EDIS_CAUSE)])
    txt = hr._format_alert(d)
    assert "#1142 BANKINDIA SELL still PLACED after 103 min" in txt and "Verify Holdings" in txt and "TPIN" in txt
    assert hr.signature(d).endswith(":cdsl_edis") and hr.signature(hr.compute_drift([], {}, [base])).endswith(":1142")
