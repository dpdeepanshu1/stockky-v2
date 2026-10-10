"""group303: the PIT guard validated a timestamp the request never carries (so it logged
'missing_prediction_timestamp' for every prediction and checked nothing), and the expected "waiting for next session" /
"too young for T+5" states were WARNINGs.

Run: cd services/decision-prediction-service/training && python3 -m pytest tests/test_group303_pit_and_log_levels.py -q
"""
import asyncio
import logging
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

app = pytest.importorskip("app")
pit = pytest.importorskip("pit_validation")


@pytest.fixture(autouse=True)
def _schema():
    app.Base.metadata.create_all(app.engine)
    yield


def _store(symbol="PITX", decision="BUY NOW"):
    body = app.PredictionSnapshotCreate(symbol=symbol, decision=decision, price=100.0)
    return asyncio.run(app.store_prediction(body, app.BackgroundTasks()))


def test_store_prediction_no_longer_reports_missing_timestamp(caplog):
    with caplog.at_level(logging.WARNING):
        resp = _store("PITA")
    assert b"STK-" in resp.body
    assert not [r for r in caplog.records if "PIT validation" in r.getMessage()], caplog.text


def test_naive_ist_timestamp_is_not_flagged_as_future_when_now_is_ist():
    ts = app.ist_now()
    ok = pit.validate_prediction_snapshot({"timestamp": ts}, now=ts)
    assert ok["ok"] is True and ok["issues"] == []
    # documents the trap this fix avoids: the validator's default clock is UTC, 5.5 h behind a naive IST stamp
    bad = pit.validate_prediction_snapshot({"timestamp": ts})
    assert "prediction_timestamp_in_future" in bad["issues"]


def test_pit_still_flags_a_genuinely_future_timestamp(monkeypatch, caplog):
    seen = {}
    real = app.validate_prediction_snapshot

    def spy(snap, now=None):
        seen["snap"], seen["now"] = snap, now
        return real(snap, now=now)

    monkeypatch.setattr(app, "validate_prediction_snapshot", spy)
    _store("PITB")
    assert seen["snap"]["timestamp"] == seen["now"]       # the stored stamp and the clock are the same IST value
    future = seen["now"].replace(year=seen["now"].year + 1)
    assert "prediction_timestamp_in_future" in real({"timestamp": future}, now=seen["now"])["issues"]


def test_calendar_age_helper_is_an_int():
    import evaluate as ev
    assert ev._calendar_age_days(None) == 0


def test_evaluate_module_logs_waiting_next_session_at_info():
    src = open(os.path.join(HERE, "evaluate.py"), encoding="utf-8").read()
    assert 'logging.INFO if scored.get("reason") == "waiting_next_session" else logging.WARNING' in src
    assert "_t5_age < 5" in src
