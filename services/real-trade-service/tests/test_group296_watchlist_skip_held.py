"""group296: the watchlist trigger does not queue a candidate for a symbol the account already holds."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from entry_engine import entry
from test_watchlist_trigger import make_row, tick


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_CHASE_PCT", "0")
    monkeypatch.delenv("ENTRY_WATCHLIST_SKIP_HELD", raising=False)
    entry._adverse_last_log.clear()
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def hold(db, symbol="TESTCO", status="OPEN", mode="DEMO"):
    db.add(models.TradePosition(mode=mode, symbol=symbol, status=status, qty_open=5, avg_entry_price=100.0,
                                opened_at=datetime.now(timezone.utc)))
    db.commit()


def quote(monkeypatch, price=103.0, symbols=("TESTCO",)):
    async def _q(syms, **_kw):
        return {s: tick(price, s) for s in syms if s in symbols}
    monkeypatch.setattr(entry, "get_quotes", _q)


def go(db, mode="DEMO"):
    return run(entry.evaluate_watchlist_entries(db, mode))


def test_a_held_symbol_is_not_queued_and_the_row_stays_active(db, monkeypatch):
    row = make_row(db)
    hold(db)
    quote(monkeypatch)
    tally = go(db)
    assert tally["queued"] == 0 and tally["held_skipped"] == 1 and tally["band_ok"] == 1
    assert db.query(models.TradeCandidate).count() == 0
    db.refresh(row)
    assert row.status == "active"


@pytest.mark.parametrize("status", ["OPEN", "PARTIALLY_CLOSED", "PENDING_EXIT"])
def test_every_held_status_counts(db, monkeypatch, status):
    make_row(db)
    hold(db, status=status)
    quote(monkeypatch)
    assert go(db)["queued"] == 0


@pytest.mark.parametrize("status", ["CLOSED", "ERROR"])
def test_a_closed_position_does_not_block(db, monkeypatch, status):
    make_row(db)
    hold(db, status=status)
    quote(monkeypatch)
    tally = go(db)
    assert tally["queued"] == 1 and "held_skipped" not in tally


def test_the_tally_has_no_new_key_when_nothing_is_held(db, monkeypatch):
    make_row(db)
    quote(monkeypatch)
    assert go(db) == {"watchlist_checked": 1, "band_ok": 1, "missed": 0, "queued": 1, "adverse": 0}


def test_only_the_held_symbol_is_skipped(db, monkeypatch):
    make_row(db, symbol="AAA")
    make_row(db, symbol="BBB")
    hold(db, "AAA")
    quote(monkeypatch, symbols=("AAA", "BBB"))
    tally = go(db)
    assert tally["queued"] == 1 and tally["held_skipped"] == 1
    assert db.query(models.TradeCandidate).one().symbol == "BBB"


def test_the_other_mode_does_not_block(db, monkeypatch):
    make_row(db)
    hold(db, mode="REAL")
    quote(monkeypatch)
    assert go(db, "DEMO")["queued"] == 1


def test_pyramiding_allowed_queues_a_held_symbol(db, monkeypatch):
    make_row(db)
    hold(db)
    db.add(models.TradeRiskConfig(mode="DEMO", allow_pyramiding=True))
    db.commit()
    quote(monkeypatch)
    assert go(db)["queued"] == 1


def test_pyramiding_off_in_config_still_skips(db, monkeypatch):
    make_row(db)
    hold(db)
    db.add(models.TradeRiskConfig(mode="DEMO", allow_pyramiding=False))
    db.commit()
    quote(monkeypatch)
    assert go(db)["queued"] == 0


@pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
def test_switch_off_restores_the_old_behaviour(db, monkeypatch, raw):
    make_row(db)
    hold(db)
    monkeypatch.setenv("ENTRY_WATCHLIST_SKIP_HELD", raw)
    quote(monkeypatch)
    assert go(db)["queued"] == 1


@pytest.mark.parametrize("raw", ["", "  ", "1", "yes"])
def test_blank_or_on_values_keep_it_on(db, monkeypatch, raw):
    make_row(db)
    hold(db)
    monkeypatch.setenv("ENTRY_WATCHLIST_SKIP_HELD", raw)
    quote(monkeypatch)
    assert go(db)["queued"] == 0


def test_a_failure_reading_positions_fails_open(db, monkeypatch):
    make_row(db)
    hold(db)

    def boom(*a, **k):
        raise RuntimeError("db")
    monkeypatch.setattr(entry, "held_exposure_positions", boom)
    quote(monkeypatch)
    assert go(db)["queued"] == 1


def test_once_the_position_closes_the_row_queues(db, monkeypatch):
    make_row(db)
    hold(db)
    quote(monkeypatch)
    assert go(db)["queued"] == 0
    db.query(models.TradePosition).update({"status": "CLOSED"})
    db.commit()
    assert go(db)["queued"] == 1


def test_the_skip_is_logged_once_per_throttle_window(db, monkeypatch, caplog):
    import logging
    make_row(db)
    hold(db)
    quote(monkeypatch)
    with caplog.at_level(logging.INFO):
        go(db)
        go(db)
    assert sum("already held" in r.getMessage() for r in caplog.records) == 1


def test_the_skip_count_adds_up_across_symbols(db, monkeypatch):
    for sym in ("AAA", "BBB", "CCC"):
        make_row(db, symbol=sym)
        hold(db, sym)
    quote(monkeypatch, symbols=("AAA", "BBB", "CCC"))
    assert go(db)["held_skipped"] == 3


def test_pyramiding_is_read_for_this_mode_only(db, monkeypatch):
    make_row(db)
    hold(db)
    db.add(models.TradeRiskConfig(mode="REAL", allow_pyramiding=True))      # the other mode allows it, DEMO does not
    db.add(models.TradeRiskConfig(mode="DEMO", allow_pyramiding=False))
    db.commit()
    quote(monkeypatch)
    assert go(db, "DEMO")["queued"] == 0
