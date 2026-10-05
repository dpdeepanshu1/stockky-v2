"""group159: one spelling per stock in the price feed and the watchlist (KOTAKBANK / KOTAKBANK.NS, M&M / M%26M).
Run: python3 -m pytest tests/test_group159_symbol_canonical.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from market_feed import feed as f
from market_feed.feed import Tick
from watchlist_engine import watchlist


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("raw,want", [
    ("KOTAKBANK", "KOTAKBANK"), ("KOTAKBANK.NS", "KOTAKBANK"), ("kotakbank.ns", "KOTAKBANK"),
    ("  TCS.BO  ", "TCS"), ("M&M", "M&M"), ("M%26M", "M&M"), ("m%26m.NS", "M&M"), ("ARE&M", "ARE&M"),
    ("ARE%26M", "ARE&M"), ("", ""), (None, ""), (".NS", ""),
])
def test_clean_sym_gives_one_spelling(raw, want):
    assert f._clean_sym(raw) == want


def test_only_trailing_suffix_is_stripped():
    assert f._clean_sym("AB.NSE") == "AB.NSE"
    assert f._clean_sym("X.NSY.NS") == "X.NSY"


@pytest.mark.parametrize("raw,want", [("M&M", "M%26M"), ("M%26M", "M%26M"), ("ARE&M", "ARE%26M"), ("TCS", "TCS")])
def test_path_sym_encodes_once(raw, want):
    assert f._path_sym(raw) == want


def test_get_quotes_fetches_each_stock_once_and_returns_every_spelling(monkeypatch):
    seen = []

    async def fake_unique(symbols, priority=False):
        seen.append(list(symbols))
        return {s: Tick(symbol=s, price=100.0, as_of=datetime.now(timezone.utc), atr=None, source="t")
                for s in symbols}
    monkeypatch.setattr(f, "_get_quotes_unique", fake_unique)
    out = run(f.get_quotes(["KOTAKBANK", "KOTAKBANK.NS", "kotakbank", "M&M", "M%26M"]))
    assert seen == [["KOTAKBANK", "M&M"]]
    assert set(out) == {"KOTAKBANK", "KOTAKBANK.NS", "kotakbank", "M&M", "M%26M"}
    assert out["KOTAKBANK.NS"] is out["KOTAKBANK"]


def test_get_quotes_passes_priority_through_and_skips_blank_symbols(monkeypatch):
    calls = []

    async def fake_unique(symbols, priority=False):
        calls.append((list(symbols), priority))
        return {}
    monkeypatch.setattr(f, "_get_quotes_unique", fake_unique)
    assert run(f.get_quotes(["", ".NS", "TCS"], priority=True)) == {}
    assert calls == [(["TCS"], True)]
    calls.clear()
    assert run(f.get_quotes(["", ".NS"])) == {}
    assert calls == []          # nothing usable: no lookup at all


def test_get_quotes_unknown_symbol_is_simply_absent(monkeypatch):
    async def fake_unique(symbols, priority=False):
        return {"A": Tick(symbol="A", price=1.0, as_of=datetime.now(timezone.utc), atr=None, source="t")}
    monkeypatch.setattr(f, "_get_quotes_unique", fake_unique)
    out = run(f.get_quotes(["A.NS", "B"]))
    assert list(out) == ["A.NS"]


# ── watchlist ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def make_row(db, symbol, ctype="results", status="active"):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type=ctype, catalyst_price=100.0,
        catalyst_ts=datetime.now(timezone.utc) - timedelta(days=1), horizon_class="mid", decay_half_life_days=12.0,
        entry_band_pct=0.07, source_tier=1, conviction_score=70.0, status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def patch_source(monkeypatch, symbol, ctype="results"):
    async def fake(db, mode):
        return [{"symbol": symbol, "catalyst_type": ctype, "catalyst_price": 100.0, "source_tier": 1,
                 "catalyst_ts": datetime.now(timezone.utc)}]
    monkeypatch.setattr(watchlist, "fetch_watchlist_candidates", fake)


def test_suffixed_source_symbol_is_stored_clean(db, monkeypatch):
    patch_source(monkeypatch, "kotakbank.ns")
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1
    assert db.query(models.WatchlistEntry).one().symbol == "KOTAKBANK"


def test_percent_encoded_source_symbol_is_stored_decoded(db, monkeypatch):
    patch_source(monkeypatch, "M%26M")
    run(watchlist.refresh_watchlist(db, "DEMO"))
    assert db.query(models.WatchlistEntry).one().symbol == "M&M"


@pytest.mark.parametrize("existing", ["KOTAKBANK", "KOTAKBANK.NS", "KOTAKBANK.BO"])
def test_active_row_in_any_spelling_blocks_a_duplicate(db, monkeypatch, existing):
    make_row(db, existing)
    patch_source(monkeypatch, "KOTAKBANK")
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 0
    assert db.query(models.WatchlistEntry).count() == 1


def test_different_catalyst_type_is_still_added(db, monkeypatch):
    make_row(db, "KOTAKBANK.NS", ctype="results")
    patch_source(monkeypatch, "KOTAKBANK", ctype="board")
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_cooldown_sees_a_legacy_suffixed_retired_row(db, monkeypatch):
    row = make_row(db, "KOTAKBANK.NS", status="expired")
    row.missed_reason = "adverse: fell -20.0% below catalyst"
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    patch_source(monkeypatch, "KOTAKBANK")
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 0
