"""
tests/test_round242_capital_board.py - round 242: Position Stocks' side of the three-way capital split (capital/capital_alloc.py).

    cd services/position-stocks-service
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
from capital import capital_alloc as ca


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL_ALLOC_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "live")
    return str(tmp_path)


def _put_board(d, mode="live", ps=45.0):
    cb.write_json(d, cb.ALLOC_FILE, {"ts": time.time(), "mode": mode, "pcts": {"real_trade": 30.0, "position_stocks": ps, "fno": 25.0}, "amounts": {}})


def test_shadow_and_off_keep_the_fixed_share(board, monkeypatch):
    _put_board(board)
    monkeypatch.setattr(config, "SCALP_POOL_CAPITAL_SHARE_PCT", 42.0)
    for m in ("shadow", "off"):
        monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", m)
        assert ca.effective_share_pct() == 42.0


def test_live_uses_the_boards_percentage_and_falls_back_without_one(board):
    assert ca.effective_share_pct() == config.SCALP_POOL_CAPITAL_SHARE_PCT      # no board yet
    _put_board(board, ps=45.0)
    assert ca.effective_share_pct() == 45.0
    _put_board(board, mode="shadow", ps=45.0)
    assert ca.effective_share_pct() == config.SCALP_POOL_CAPITAL_SHARE_PCT      # the board is not live


def test_f_and_o_exposure_counts_only_in_live(board, monkeypatch):
    cb.write_stats(board, "fno", net_pnl=0, trades=0, capital_base=0, exposure=800.0, active=True)
    assert ca.extra_peer_exposure() == 800.0
    cb.write_stats(board, "fno", net_pnl=0, trades=0, capital_base=0, exposure=800.0, active=True, now=time.time() - 5000)
    assert ca.extra_peer_exposure() == 0.0                                       # an old figure is not counted
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "shadow")
    cb.write_stats(board, "fno", net_pnl=0, trades=0, capital_base=0, exposure=800.0, active=True)
    assert ca.extra_peer_exposure() == 0.0


class _NoDb:
    def query(self, *a, **k):
        raise RuntimeError("no db")


def test_publish_never_raises_and_is_silent_when_off(board, monkeypatch):
    ca.publish(_NoDb(), 500.0)                                                   # unreadable db: nothing written, no exception
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "off")
    ca.publish(_NoDb(), 500.0)
    assert cb.read_stats(board, "position_stocks") is None


def test_status_reports_what_this_pool_uses(board):
    _put_board(board, ps=45.0)
    s = ca.status()
    assert s["effective_share_pct"] == 45.0 and s["why"] == "board" and s["fixed_share_pct"] == config.SCALP_POOL_CAPITAL_SHARE_PCT


def test_own_stats_reads_net_pnl_from_the_charges_ledger():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import models
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    for i, (day, net) in enumerate((("2026-11-05", 100.0), ("2026-11-06", -30.0), ("2026-01-01", 5000.0))):
        db.add(models.ScalpChargesLedger(position_id=i + 1, symbol="X", day=day, net_pnl=net))
    db.commit()
    assert ca.own_stats(db, today="2026-11-08", window_days=30) == (70.0, 2)
