"""Round 671 (D3 check): scripts/diag_wait_dedupe.py runs on real models, counts repeats and leaves DEMO rows out.
Run from services/real-trade-service:   python3 -m pytest tests/test_round671_diag_wait_dedupe.py -q"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts", "diag_wait_dedupe.py")


def _run(monkeypatch, fill):
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "15")      # other tests may leave another value in the environment
    monkeypatch.delenv("WINDOW_MIN", raising=False)
    import db
    import models
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    now = datetime.now(timezone.utc)
    with Session(eng) as s:
        fill(s, models, now)
        s.commit()
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(open(_SCRIPT, encoding="utf-8").read(), _SCRIPT, "exec"), {"__name__": "__main__"})
    return buf.getvalue()


def _row(models, now, sym, action, why, mins_ago, mode="REAL"):
    return models.TradeDecision(mode=mode, symbol=sym, decision_type="ENTRY", action=action, reasoning=why,
                                created_at=now - timedelta(minutes=mins_ago))


def test_repeats_are_counted_and_demo_is_left_out(monkeypatch):
    def fill(s, m, now):
        for i in range(6):
            s.add(_row(m, now, "ABB", "WAIT", "no_setup_window", 3 * i))
        s.add(_row(m, now, "CAT", "WAIT", "late_day", 5))
        s.add(_row(m, now, "CAT", "ENTER", "ok", 1))
        s.add(_row(m, now, "ZZZ", "WAIT", "x", 0, mode="DEMO"))
    out = _run(monkeypatch, fill)
    assert "WAIT     rows=     7  symbols=    2" in out
    assert "ENTER    rows=     1" in out
    assert "ZZZ" not in out
    assert "6 rows  smallest gap   3.0 min  ABB" in out
    assert "again inside 13.5 min: 5" in out


def test_clean_dedupe_reports_zero_repeats(monkeypatch):
    def fill(s, m, now):
        s.add(_row(m, now, "ABB", "WAIT", "no_setup_window", 20))
        s.add(_row(m, now, "ABB", "WAIT", "no_setup_window", 1))
    out = _run(monkeypatch, fill)
    assert "again inside 13.5 min: 0" in out


def test_empty_table_prints_no_rows_and_does_not_fail(monkeypatch):
    out = _run(monkeypatch, lambda s, m, now: None)
    assert "(no rows)" in out and "(no WAIT rows in the window)" in out
