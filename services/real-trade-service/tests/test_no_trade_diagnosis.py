"""group 226: scripts/no_trade_diagnosis.py groups the day's ENTRY/EXIT decisions by reason (read-only)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import db as dbmod  # noqa: E402
import models  # noqa: E402
import no_trade_diagnosis as nd  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _utc_naive(ist_dt):
    return ist_dt.astimezone(timezone.utc).replace(tzinfo=None)


def test_normalize_reason_blanks_numbers_and_symbol_prefix():
    a = nd.normalize_reason("R:R 1.81:1 below floor 2.0:1 - setup does not offer enough reward")
    b = nd.normalize_reason("R:R 1.79:1 below floor 2.0:1 - setup does not offer enough reward")
    assert a == b and "#" in a
    assert nd.normalize_reason(None) == "(no reason recorded)"
    assert nd.normalize_reason("ABC: No current price available") == "No current price available"
    assert len(nd.normalize_reason("x" * 500)) == 110


def test_day_window_is_the_ist_day_in_utc():
    s, e, d = nd.day_window_utc("2026-10-07")
    assert s == datetime(2026, 10, 6, 18, 30) and e == datetime(2026, 10, 7, 18, 30) and str(d) == "2026-10-07"
    assert nd.day_window_utc(None)[2] is not None


def test_group_reasons_counts():
    c = nd.group_reasons([("WAIT", "R:R 1.8"), ("WAIT", "R:R 1.9"), ("HOLD", "x")])
    assert c[("WAIT", "R:R #")] == 2 and c[("HOLD", "x")] == 1


@pytest.fixture()
def seeded(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: Session)
    day = datetime(2026, 10, 7, 11, 0, tzinfo=IST)
    t = _utc_naive(day)
    s = Session()
    s.add(models.TradeGateState(mode="REAL", armed=True, admin_authenticated=True, dhan_connected=True,
                                risk_config_confirmed=True, auto_pilot_enabled=True))
    s.add(models.TradeCandidate(mode="REAL", symbol="AAA", source_tab="hot_picks", decision_label="BUY NOW",
                                conviction_score=70, signal_price=10, received_at=t, consumed=True))
    s.add(models.TradeCandidate(mode="REAL", symbol="BBB", source_tab="ipo", decision_label="BUY NOW",
                                conviction_score=60, signal_price=10, received_at=t, consumed=False))
    for i in range(3):
        s.add(models.TradeDecision(mode="REAL", symbol=f"S{i}", decision_type="ENTRY", action="WAIT",
                                   reasoning=f"R:R 1.{i}:1 below floor 2.0:1", created_at=t))
    s.add(models.TradeDecision(mode="REAL", symbol="IOC", decision_type="EXIT", action="HOLD",
                               reasoning="No current price available this cycle - skipping evaluation.", created_at=t))
    s.add(models.TradeDecision(mode="REAL", symbol="OLD", decision_type="ENTRY", action="WAIT",
                               reasoning="yesterday", created_at=t - timedelta(days=1)))
    s.add(models.TradeOrder(mode="REAL", symbol="AAA", side="BUY", qty=1, status="REJECTED", created_at=t))
    s.commit()
    s.close()
    return Session


def test_report_names_the_gate_that_stopped_the_buys_and_the_exit_problem(seeded, capsys):
    assert nd.main(["--mode", "REAL", "--date", "2026-10-07"]) == 0
    out = capsys.readouterr().out
    assert "armed=True" in out and "auto_pilot_enabled=True" in out
    assert "candidates queued today: 2 (evaluated/consumed: 1)" in out
    assert "ENTRY decisions today (3)" in out and "R:R #:# below floor #:#" in out
    assert "EXIT decisions today (1)" in out and "No current price available this cycle" in out
    assert "yesterday" not in out                                  # other IST days are excluded
    assert "orders created today: 1" in out and "BUY   REJECTED" in out
    assert "open positions now: 0" in out


def test_empty_day_and_missing_gate_row_do_not_crash(monkeypatch, capsys):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng)
    monkeypatch.setattr(dbmod, "get_session_factory", lambda: sessionmaker(bind=eng, future=True))
    assert nd.main(["--mode", "DEMO", "--date", "2026-10-07", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert "no TradeGateState row" in out and "(none)" in out
