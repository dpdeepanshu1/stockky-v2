"""Session 72 (#10) regression tests for training/evaluate.py + trades.py. Run: cd services/decision-prediction-service/training && python -m pytest tests -q"""
import os, sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import pytest
import evaluate as ev


def bars(*closes, start=date(2026, 9, 1)):
    return [{"date": start + timedelta(days=i), "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(closes)]


def pred(day, decision="BUY NOW"):
    return SimpleNamespace(symbol="X", timestamp=datetime(day.year, day.month, day.day, 10, 0), decision=decision,
                           entry_range_low=None, price=100.0)


def test_history_backfill_is_off_by_default(monkeypatch):
    b = bars(100, 101, 99, 103)                       # last bar == prediction day: no next session yet
    p = pred(b[-1]["date"])
    monkeypatch.setattr(ev, "_ALLOW_HISTORY_BACKFILL", False)
    out = ev._evaluate_t1_with_backfill(p, b, allow_backfill=True)
    assert out["ok"] is False and out["reason"] == "waiting_next_session"     # was: labelled from bars[-2]->bars[-1], a PRE-prediction move
    monkeypatch.setattr(ev, "_ALLOW_HISTORY_BACKFILL", True)
    assert ev._evaluate_t1_with_backfill(p, b, allow_backfill=True)["mode"] == "history_backfill"


def test_real_next_session_still_scores(monkeypatch):
    monkeypatch.setattr(ev, "_ALLOW_HISTORY_BACKFILL", False)
    b = bars(100, 101, 99, 103)
    out = ev._evaluate_t1_with_backfill(pred(b[1]["date"]), b, allow_backfill=True)
    assert out["ok"] and out["mode"] == "realtime" and out["entry_price"] == 101 and out["t1_close"] == 99 and out["success"] is False


@pytest.mark.parametrize("decision,ret,expected", [
    ("DO NOT BUY", 4.0, 0),     # stock rose after we said avoid -> NOT a win (was scored 1)
    ("DO NOT BUY", -3.0, 1),
    ("SELL", -1.0, 1), ("AVOID / WAIT", 2.0, 0),
    ("BUY NOW", 3.0, 1), ("PREPARE TO BUY", 1.0, 0), ("HOLD", 1.0, 1), ("HOLD", -1.0, 0),
])
def test_t5_scoring(decision, ret, expected):
    assert ev._score_t5(decision, 0, 1 if ret > 0 else 0, ret) == expected


def test_zero_price_bars_are_dropped(monkeypatch):
    import httpx
    payload = {"candles": [{"date": "2026-09-01", "open": 100, "high": 101, "low": 99, "close": 100},
                           {"date": "2026-09-02", "open": 0, "high": 0, "low": 0, "close": 0},          # missing data
                           {"date": "2026-09-03", "open": 100, "high": None, "low": None, "close": 102}]}
    monkeypatch.setattr(ev, "MARKET_DATA_URL", "http://x")
    monkeypatch.setattr(ev, "_yf_is_rate_limited", lambda: True)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    rows = ev._fetch_bars("X", None, None)
    assert [r["date"].day for r in rows] == [1, 3]
    assert rows[1]["low"] == 102 and rows[1]["high"] == 102          # falls back to close, not 0.0


def test_weekly_review_fires_even_if_the_exact_day_7_sweep_was_missed(monkeypatch):
    import models as m, trades as tr
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine("sqlite:///:memory:"); m.Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(tr, "SessionLocal", S); monkeypatch.setattr(tr, "_ensure_trade_tables", lambda: None)
    monkeypatch.setattr(tr, "_fetch_latest_price", lambda sym: 106.0)
    db = S()
    db.add(m.PortfolioAccount(id=1, cash_balance=1000.0, total_deposited=100000.0, realized_pnl=0.0, updated_at=tr.ist_now()))
    db.add(m.PaperTrade(trade_id="T1", prediction_id="P1", symbol="X", capital_allocated=1000.0, entry_price=100.0, quantity=10,
                        entry_date=tr.ist_now() - timedelta(days=8, hours=1), target=None, stop_loss=None, max_holding_days=21,
                        weeks_held=0, status="OPEN", current_price=100.0, last_marked_at=tr.ist_now(), pnl_amount=0.0, pnl_pct=0.0,
                        created_at=tr.ist_now()))
    db.commit(); db.close()
    t = tr.mark_to_market("T1")
    assert t.status == "CLOSED" and t.exit_reason == "weekly_review_profit_take"     # day 8: old code needed days_held % 7 == 0
