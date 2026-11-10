"""Round 296: (#8) a NOT_FILLED entry is not a trade or a loss; (#10) dropping borrowed Dhan credentials restores the base URL too."""
from __future__ import annotations

import types

import pytest

from fno import creds, paper, store as st
from fno.models import ContractRef
from fno.params import Params
from test_engine_paper import NOW


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _row(db, strategy="CORE", mode="REAL"):
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 1, 100.0, NOW.timestamp(), Params(), mode=mode)
    p.strategy = strategy
    db.insert_position(p)
    return p


def _close(db, p, reason, net):
    p.status, p.exit_ts, p.exit_reason = paper.CLOSED, NOW.timestamp() + 60, reason
    p.gross_pnl, p.charges, p.net_pnl = net, 0.0, net
    db.update_position(p)


def _day(db):
    return db.day_summary(st.day_of(NOW.timestamp()), "REAL")


def test_an_unfilled_entry_is_not_a_trade_a_loss_or_a_strategy_entry(db):
    _close(db, _row(db, "ORB"), "NOT_FILLED", 0.0)
    s = _day(db)
    assert s["trades"] == 0 and s["losses"] == 0 and s["wins"] == 0
    assert s["by_strategy"] == {}
    assert s["realized_net"] == 0.0


def test_a_real_trade_next_to_an_unfilled_one_still_counts_once(db):
    _close(db, _row(db), "NOT_FILLED", 0.0)
    _close(db, _row(db), "SL", -500.0)
    _close(db, _row(db), "TARGET", 800.0)
    s = _day(db)
    assert s["trades"] == 2 and s["wins"] == 1 and s["losses"] == 1 and s["realized_net"] == 300.0
    assert s["by_strategy"]["CORE"]["entries"] == 2


def test_an_unfilled_entry_does_not_use_up_the_daily_trade_slot(db):
    _close(db, _row(db), "NOT_FILLED", 0.0)
    assert _day(db)["trades"] < Params().max_trades_per_day or Params().max_trades_per_day == 0


def test_a_genuine_zero_pnl_trade_is_still_a_trade(db):
    _close(db, _row(db), "TIME", 0.0)
    s = _day(db)
    assert s["trades"] == 1 and s["losses"] == 1


def test_drop_restores_the_previous_base_url():
    cfg = types.SimpleNamespace(DHAN_CLIENT_ID="", DHAN_ACCESS_TOKEN="", DHAN_BASE_URL="https://api.dhan.co/v2")
    prev = creds.apply(cfg, {"client_id": "C", "access_token": "T", "base_url": "https://sandbox.dhan.co/v2/"})
    assert cfg.DHAN_BASE_URL == "https://sandbox.dhan.co/v2"
    creds.drop(cfg, prev)
    assert (cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN, cfg.DHAN_BASE_URL) == ("", "", "https://api.dhan.co/v2")


def test_drop_accepts_the_old_two_item_form_and_leaves_the_url_alone():
    cfg = types.SimpleNamespace(DHAN_CLIENT_ID="C", DHAN_ACCESS_TOKEN="T", DHAN_BASE_URL="https://x")
    creds.drop(cfg, ("", ""))
    assert (cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN, cfg.DHAN_BASE_URL) == ("", "", "https://x")


def test_a_borrow_that_sends_no_base_url_changes_nothing_to_restore():
    cfg = types.SimpleNamespace(DHAN_CLIENT_ID="", DHAN_ACCESS_TOKEN="", DHAN_BASE_URL="https://api.dhan.co/v2")
    prev = creds.apply(cfg, {"client_id": "C", "access_token": "T"})
    creds.drop(cfg, prev)
    assert cfg.DHAN_BASE_URL == "https://api.dhan.co/v2"
