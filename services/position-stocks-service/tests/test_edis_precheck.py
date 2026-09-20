"""
tests/test_edis_precheck.py — unit tests for the eDIS/CDSL TPIN pre-check
added in session72 (issue #17) to eod_squareoff._run_edis_precheck().

The pre-check fires when at least one position in the squareoff batch has
overnight_converted_to_cnc=True. It calls
execution.dhan_client.edis_verification_summary() and:
  - emits WARNING + notifier.notify_critical when verified_today is False
  - emits WARNING (no critical alert) when verified_today is None
  - emits INFO log when verified_today is True
  - does NOT raise — the SELL attempt always proceeds regardless

Patch strategy: _run_edis_precheck calls `notifier.notify_critical()` via
the module-level `import notifier` that eod_squareoff already has at the top
of the file. Tests patch `notifier.notify_critical` directly (same pattern
as test_overnight_stop.py) so the patch actually intercepts the call.

All tests are offline (SQLite in-memory, monkeypatched Dhan calls).

Run from services/position-stocks-service:
    python -m pytest tests/test_edis_precheck.py -v
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
import notifier
from execution import dhan_client
import orders.eod_squareoff as sq

_engine = create_engine("sqlite:///:memory:")


def _make_pos(db, *, symbol="TESTCNC", overnight=True):
    pos = models.ScalpPosition(
        symbol=symbol,
        dhan_security_id="999",
        window_source="5m",
        status="OPEN",
        entry_price=100.0,
        quantity=10,
        target_price=105.0,
        stop_price=97.0,
        adaptive_target_pct=5.0,
        adaptive_stop_pct=3.0,
        capital_risked=1000.0,
        opened_at=datetime.now(timezone.utc),
        overnight_converted_to_cnc=overnight,
    )
    db.add(pos)
    db.commit()
    return pos


@pytest.fixture()
def env(monkeypatch):
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    db = sessionmaker(bind=_engine)()

    # Capture notify_critical calls via the module-level notifier object that
    # eod_squareoff uses — patching notifier.notify_critical intercepts the
    # call regardless of where in the call stack it originates.
    notifications = []
    monkeypatch.setattr(notifier, "notify_critical", lambda msg: notifications.append(msg))

    # Stub eDIS check to a safe default (no-op) — individual tests override as needed.
    monkeypatch.setattr(
        dhan_client, "edis_verification_summary",
        lambda db_: {"verified_today": True, "pending_symbols": []},
    )

    yield db, notifications
    db.close()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_no_cnc_positions_skips_check(env, monkeypatch):
    """No overnight_converted_to_cnc positions → edis_verification_summary never called."""
    db, notifications = env
    pos = _make_pos(db, overnight=False)
    called = {"n": 0}

    def _boom(db_):
        called["n"] += 1
        return {}

    monkeypatch.setattr(dhan_client, "edis_verification_summary", _boom)
    sq._run_edis_precheck(db, [pos])

    assert called["n"] == 0
    assert not notifications


def test_verified_true_logs_info_no_alert(env, caplog):
    """verified_today=True → INFO log, no notify_critical call."""
    db, notifications = env
    pos = _make_pos(db, overnight=True)

    with caplog.at_level(logging.INFO, logger="eod_squareoff"):
        sq._run_edis_precheck(
            db, [pos],
            _edis_override={"verified_today": True, "pending_symbols": []},
        )

    assert not notifications
    assert any("verified" in r.message.lower() for r in caplog.records)


def test_verified_false_sends_critical_alert(env, caplog):
    """verified_today=False → WARNING log + notify_critical fired."""
    db, notifications = env
    pos = _make_pos(db, overnight=True)

    with caplog.at_level(logging.WARNING, logger="eod_squareoff"):
        sq._run_edis_precheck(
            db, [pos],
            _edis_override={"verified_today": False, "pending_symbols": ["TESTCNC"]},
        )

    assert notifications, "Expected notify_critical to fire but got no notifications"
    assert any("CDSL" in n or "eDIS" in n for n in notifications)
    assert any(
        "eDIS" in r.message or "TPIN" in r.message or "CDSL" in r.message
        for r in caplog.records
    )


def test_verified_none_warns_but_no_critical(env, caplog):
    """verified_today=None (inconclusive shape) → WARNING log, no critical alert."""
    db, notifications = env
    pos = _make_pos(db, overnight=True)

    with caplog.at_level(logging.WARNING, logger="eod_squareoff"):
        sq._run_edis_precheck(
            db, [pos],
            _edis_override={
                "verified_today": None,
                "detail": "unrecognized shape",
                "pending_symbols": [],
            },
        )

    assert not notifications, "verified_today=None must not fire notify_critical"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_edis_call_exception_is_non_fatal(env, monkeypatch):
    """If edis_verification_summary() itself throws, _run_edis_precheck must not raise."""
    db, notifications = env
    pos = _make_pos(db, overnight=True)

    monkeypatch.setattr(
        dhan_client, "edis_verification_summary",
        lambda db_: (_ for _ in ()).throw(RuntimeError("Dhan timeout")),
    )
    # Must not propagate — the flat SELL should still be attempted by the caller
    sq._run_edis_precheck(db, [pos])
    assert not notifications  # exception path → warning log only, never a critical alert


def test_multiple_cnc_positions_symbols_appear_in_alert(env):
    """All CNC positions' symbols must be named in the notify_critical message."""
    db, notifications = env
    pos1 = _make_pos(db, symbol="ALPHA", overnight=True)
    pos2 = _make_pos(db, symbol="BETA", overnight=True)

    sq._run_edis_precheck(
        db, [pos1, pos2],
        _edis_override={"verified_today": False, "pending_symbols": ["ALPHA", "BETA"]},
    )

    assert notifications
    alert = notifications[0]
    assert "ALPHA" in alert and "BETA" in alert


def test_mixed_positions_only_cnc_ones_counted(env, monkeypatch):
    """A non-CNC position alongside a CNC one must not change the CNC count in the alert."""
    db, notifications = env
    cnc_pos = _make_pos(db, symbol="OVERNIGHT", overnight=True)
    intraday_pos = _make_pos(db, symbol="INTRADAY", overnight=False)

    sq._run_edis_precheck(
        db, [cnc_pos, intraday_pos],
        _edis_override={"verified_today": False, "pending_symbols": []},
    )

    assert notifications
    alert = notifications[0]
    # "1 position(s)" — only the CNC one was counted
    assert "1 position" in alert
    # The intraday symbol must not appear in the alert
    assert "INTRADAY" not in alert
