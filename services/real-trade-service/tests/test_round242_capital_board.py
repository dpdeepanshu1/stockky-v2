"""
tests/test_round242_capital_board.py - round 242: Real Trade's side of the three-way capital split (execution/capital_alloc.py).

    cd services/real-trade-service
    python -m pytest tests/test_round242_capital_board.py -v
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import capital_board as cb
import config
from execution import capital_alloc as ca


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL_ALLOC_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "live")
    return str(tmp_path)


def _put_board(d, mode="live", real=40.0):
    cb.write_json(d, cb.ALLOC_FILE, {"ts": time.time(), "mode": mode, "pcts": {"real_trade": real, "position_stocks": 35.0, "fno": 25.0}, "amounts": {}})


def test_shadow_and_off_keep_the_fixed_share(board, monkeypatch):
    _put_board(board)
    for m in ("shadow", "off"):
        monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", m)
        assert ca.effective_share_pct() == config.CAPITAL_SHARE_PCT
        assert ca.effective_share_pct(40.0) == 40.0                      # the caller's own fixed value (the risk engine passes its constant)


def test_live_uses_the_boards_percentage(board):
    _put_board(board, real=40.0)
    assert ca.effective_share_pct(50.0) == 40.0


def test_live_without_a_fresh_or_live_board_falls_back_to_fixed(board):
    assert ca.effective_share_pct(50.0) == 50.0                          # no board
    _put_board(board, mode="shadow")
    assert ca.effective_share_pct(50.0) == 50.0                          # the board itself is not live
    cb.write_json(board, cb.ALLOC_FILE, {"ts": time.time() - 99 * 3600, "mode": "live", "pcts": {"real_trade": 40.0}})
    assert ca.effective_share_pct(50.0) == 50.0                          # too old


def test_f_and_o_exposure_counts_only_in_live(board, monkeypatch):
    cb.write_stats(board, "fno", net_pnl=0, trades=0, capital_base=0, exposure=1500.0, active=True)
    assert ca.extra_peer_exposure() == 1500.0
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "shadow")
    assert ca.extra_peer_exposure() == 0.0                               # shadow changes nothing


class _NoDb:
    def query(self, *a, **k):
        raise RuntimeError("no db")


def test_sync_publishes_stats_and_writes_the_board_even_when_the_db_is_unreadable(board, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "shadow")
    cb.write_stats(board, "position_stocks", net_pnl=0, trades=0, capital_base=1000, exposure=2000.0, active=True)
    doc = ca.sync(_NoDb(), free_cash=7000.0, own_exposure=1000.0, capital_base=8000.0)
    assert doc and doc["mode"] == "shadow" and doc["total_equity"] == 10000.0
    assert doc["active"] == ["real_trade", "position_stocks"]            # F&O has not said it is active: its part goes to the other two
    assert cb.read_stats(board, "real_trade")["exposure"] == 1000.0
    assert doc["pcts"]["fno"] == 0.0 and abs(sum(doc["pcts"].values()) - 100) < 1e-3


def test_sync_is_silent_when_off_and_never_raises(board, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "off")
    assert ca.sync(_NoDb(), 1.0, 1.0, 1.0) is None
    assert cb.read_stats(board, "real_trade") is None
    monkeypatch.setattr(config, "CAPITAL_ALLOC_DIR", "/proc/definitely/not/writable")
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "live")
    ca.sync(_NoDb(), 1.0, 1.0, 1.0)                                      # an unwritable folder must not raise


def test_status_reports_what_this_service_uses(board):
    _put_board(board, real=40.0)
    s = ca.status()
    assert s["mode"] == "live" and s["effective_share_pct"] == 40.0 and s["why"] == "board" and s["board"]["pcts"]["real_trade"] == 40.0


def test_own_stats_reads_net_pnl_and_closed_trades():
    pytest.importorskip("sqlalchemy")
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import models
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add_all([models.TradePnlDaily(mode="REAL", day="2026-11-05", net_realized=500.0), models.TradePnlDaily(mode="REAL", day="2026-11-06", net_realized=-200.0),
                models.TradePnlDaily(mode="DEMO", day="2026-11-06", net_realized=9999.0), models.TradePnlDaily(mode="REAL", day="2026-01-01", net_realized=777.0)])
    db.add_all([models.TradePosition(mode="REAL", symbol="A", status="CLOSED", avg_entry_price=1.0, closed_at=datetime(2026, 11, 5, 10)),
                models.TradePosition(mode="REAL", symbol="B", status="OPEN", avg_entry_price=1.0),
                models.TradePosition(mode="DEMO", symbol="C", status="CLOSED", avg_entry_price=1.0, closed_at=datetime(2026, 11, 5, 10))])
    db.commit()
    assert ca.own_stats(db, today="2026-11-08", window_days=30) == (300.0, 1)     # REAL only, inside the window only
