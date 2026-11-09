"""F&O round 242: REAL sizing comes from the three-way capital board (never FNO_DEMO_CAPITAL), no board = no REAL entry, DEMO unchanged, and F&O publishes its figures for the board."""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capital_board
from fno import capital, paper
from fno.store import Store
from test_engine_paper import NOW, FakeBroker, engine, make_cfg


@pytest.fixture
def db():
    s = Store(":memory:")
    yield s
    s.close()


def real_cfg(amount, **kw):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1000", DHAN_ACCESS_TOKEN="tok", **kw)
    capital_board.write_json(cfg.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE, {"ts": time.time(), "mode": "shadow", "pcts": {"fno": 25.0}, "amounts": {"fno": amount}})
    return cfg


def go_real(db, cfg):
    b = FakeBroker()
    e = engine(db, cfg=cfg, broker=b)
    db.kv_set("fno_armed_day", "2026-11-10")
    e.tick(NOW)
    return e, b


def test_real_sizes_from_the_board_amount_not_from_demo_capital(db):
    big, small = real_cfg(500000.0), real_cfg(40000.0)
    assert big.FNO_DEMO_CAPITAL == 500000.0
    _, b1 = go_real(db, big)
    lots_big = b1.sent[0]["quantity"]
    db2 = Store(":memory:")
    _, b2 = go_real(db2, small)
    lots_small = b2.sent[0]["quantity"] if b2.sent else 0
    db2.close()
    assert lots_big > lots_small                                      # a smaller board amount sizes smaller (or not at all), whatever FNO_DEMO_CAPITAL says


def test_real_without_a_fresh_board_sends_nothing_and_says_why(db):
    cfg = real_cfg(500000.0)
    os.remove(os.path.join(cfg.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE))
    e, b = go_real(db, cfg)
    assert b.sent == [] and db.open_positions() == []
    ev = [x for x in db.events() if x["kind"] == "skip" and x.get("stage") == "capital"]
    assert ev and "capital board" in ev[0]["reasons"][0]


def test_real_with_a_stale_board_sends_nothing(db):
    cfg = real_cfg(500000.0)
    capital_board.write_json(cfg.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE, {"ts": time.time() - 10 * 3600, "mode": "shadow", "pcts": {"fno": 25.0}, "amounts": {"fno": 500000.0}})
    e, b = go_real(db, cfg)
    assert b.sent == []


def test_real_with_capital_alloc_off_sends_nothing(db):
    cfg = real_cfg(500000.0, CAPITAL_ALLOC_MODE="off")
    e, b = go_real(db, cfg)
    assert b.sent == [] and "off" in e.capital_why


def test_demo_still_uses_demo_capital_and_needs_no_board(db):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY")
    os.remove(os.path.join(cfg.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE))
    e = engine(db, cfg=cfg)
    e.tick(NOW)
    assert e.params().capital == cfg.FNO_DEMO_CAPITAL
    assert db.open_positions()                                        # the DEMO entry still happens


def test_publish_writes_stats_with_exposure_and_active_flag(db):
    cfg = real_cfg(500000.0)
    e, b = go_real(db, cfg)                                           # one REAL position is now open
    pos = db.open_positions("REAL")[0]
    assert capital.publish(db, cfg, "2026-11-10", now=NOW.timestamp(), force=True)
    s = capital_board.read_stats(cfg.CAPITAL_ALLOC_DIR, "fno")
    assert s["active"] is True and abs(s["exposure"] - pos.entry_premium * pos.qty) < 0.01 and s["trades"] == 0
    capital.publish(db, cfg, "2020-01-01", now=NOW.timestamp(), force=True)           # not armed today -> not active: its part goes to the other two
    assert capital_board.read_stats(cfg.CAPITAL_ALLOC_DIR, "fno")["active"] is False


def test_publish_counts_only_closed_real_net_pnl_in_the_window(db):
    cfg = real_cfg(500000.0)
    e, b = go_real(db, cfg)
    pos = db.open_positions("REAL")[0]
    pos.status, pos.net_pnl = paper.CLOSED, 1234.5
    db.update_position(pos)
    s = capital.stats(db, cfg, today="2026-11-10")
    assert s["net_pnl"] == 1234.5 and s["trades"] == 1 and s["exposure"] == 0.0
    assert capital.stats(db, cfg, today="2027-06-01")["trades"] == 0      # older than the window


def test_publish_is_throttled_and_silent_when_off(db):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", CAPITAL_ALLOC_MODE="off")
    assert capital.publish(db, cfg, None, force=True) is False
    assert capital_board.read_stats(cfg.CAPITAL_ALLOC_DIR, "fno") is None
    cfg2 = make_cfg(FNO_UNDERLYINGS="NIFTY")
    t = time.time()
    assert capital.publish(db, cfg2, None, now=t, force=True) is True
    assert capital.publish(db, cfg2, None, now=t + 5) is False            # inside the 30 s throttle


def test_status_shows_board_and_real_capital(db):
    cfg = real_cfg(123456.0)
    s = capital.status(cfg, None, "2026-11-10")
    assert s["real_sizing_capital"] == 123456.0 and s["demo_capital"] == 500000.0 and s["board"]["amounts"]["fno"] == 123456.0
