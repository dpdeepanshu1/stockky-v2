"""group169 (item 5, rest): penny / ETF watchlist rows are retired instead of re-polled, and the per-row
SKIPPED / EXPIRED INFO lines are folded into one summary line per cycle.
Run: python3 -m pytest tests/test_group169_watchlist_instrument_retire_summary_log.py -q"""
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
from entry_engine import entry
from market_feed.feed import Tick
from watchlist_engine import watchlist


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_INSTRUMENT_RETIRE", "WATCHLIST_ETF_SYMBOLS",
              "CANDIDATE_MIN_STOCK_PRICE", "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_DROP_COOLDOWN_HOURS",
              "WATCHLIST_INDEX_FILTER", "WATCHLIST_ONE_ROW_PER_SYMBOL"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()


def make_row(db, *, symbol, catalyst_price=100.0, source_tier=1, ctype="results"):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type=ctype, catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=3.0,
        entry_band_pct=0.07, source_tier=source_tier, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def patch_quotes(monkeypatch, prices):
    async def fake(symbols, **kw):
        return {s: Tick(symbol=s, price=p, as_of=datetime.now(timezone.utc), atr=1.5, source="test")
                for s, p in prices.items() if s in symbols}
    monkeypatch.setattr(entry, "get_quotes", fake)


def evaluate(db):
    return run(entry.evaluate_watchlist_entries(db, "DEMO"))


# ── penny rows are retired ───────────────────────────────────────────────────
def test_penny_row_is_expired_with_instrument_prefix(db, monkeypatch):
    row = make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"HARDWYN": 12.3})
    tally = evaluate(db)
    db.refresh(row)
    assert row.status == "expired" and row.missed_reason.startswith(entry.INSTRUMENT_EXPIRE_PREFIX)
    assert tally["adverse"] == 1 and tally["instrument_expired"] == 1 and tally["queued"] == 0


def test_retired_penny_row_is_not_priced_again(db, monkeypatch):
    make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"HARDWYN": 12.3})
    evaluate(db)
    assert evaluate(db)["watchlist_checked"] == 0


def test_price_at_floor_still_queues(db, monkeypatch):
    make_row(db, symbol="OKCO", catalyst_price=20.0)
    patch_quotes(monkeypatch, {"OKCO": 20.0})
    t = evaluate(db)
    assert t["queued"] == 1 and "instrument_expired" not in t


def test_normal_stock_tally_has_no_new_keys(db, monkeypatch):
    make_row(db, symbol="INFY", catalyst_price=1500.0)
    patch_quotes(monkeypatch, {"INFY": 1505.0})
    assert set(evaluate(db)) == {"watchlist_checked", "band_ok", "missed", "queued", "adverse"}


def test_floor_follows_env(db, monkeypatch):
    monkeypatch.setenv("CANDIDATE_MIN_STOCK_PRICE", "5")
    make_row(db, symbol="CHEAPCO", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"CHEAPCO": 12.0})
    assert evaluate(db)["queued"] == 1


# ── ETF rows are retired before any price lookup ─────────────────────────────
def test_etf_row_expired_without_a_quote_call(db, monkeypatch):
    row = make_row(db, symbol="MAFANG", catalyst_price=100.0)
    calls = []

    async def fake(symbols, **kw):
        calls.append(list(symbols))
        return {}
    monkeypatch.setattr(entry, "get_quotes", fake)
    t = evaluate(db)
    db.refresh(row)
    assert row.status == "expired" and row.missed_reason.startswith("instrument: ")
    assert t["instrument_expired"] == 1 and t["watchlist_checked"] == 0
    assert calls == []


def test_etf_in_env_list_is_retired(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_ETF_SYMBOLS", "ABCFUND")
    row = make_row(db, symbol="ABCFUND")
    patch_quotes(monkeypatch, {})
    evaluate(db)
    db.refresh(row)
    assert row.status == "expired"


# ── switches ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("env", ["WATCHLIST_INSTRUMENT_RETIRE", "WATCHLIST_ADVERSE_GUARD"])
def test_off_switches_keep_group157_hold_back(db, monkeypatch, env):
    monkeypatch.setenv(env, "0")
    pen = make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"HARDWYN": 12.0})
    t = evaluate(db)
    db.refresh(pen)
    assert pen.status == "active" and "instrument_expired" not in t
    # with the whole guard off the penny row is simply queued as before group155
    assert t["queued"] == (1 if env == "WATCHLIST_ADVERSE_GUARD" else 0)


# ── refresh: cooldown and ETF names ──────────────────────────────────────────
def patch_source(monkeypatch, items):
    async def fake(db, mode):
        return items
    monkeypatch.setattr(watchlist, "fetch_watchlist_candidates", fake)


def cand(sym, ctype="results", price=15.0, tier=1):
    return {"symbol": sym, "catalyst_type": ctype, "catalyst_price": price, "catalyst_price_source": "live",
            "catalyst_ts": None, "source_tier": tier, "conviction_score": 70.0}


def test_retired_penny_symbol_not_re_added_within_cooldown(db, monkeypatch):
    make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"HARDWYN": 12.0})
    evaluate(db)
    patch_source(monkeypatch, [cand("HARDWYN")])
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 0


def test_cooldown_zero_allows_re_add(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_DROP_COOLDOWN_HOURS", "0")
    make_row(db, symbol="HARDWYN", catalyst_price=12.0)
    patch_quotes(monkeypatch, {"HARDWYN": 12.0})
    evaluate(db)
    patch_source(monkeypatch, [cand("HARDWYN")])
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_etf_names_are_never_inserted(db, monkeypatch, caplog):
    patch_source(monkeypatch, [cand("NIFTYBEES", price=250.0), cand("MAFANG", price=100.0), cand("INFY", price=1500.0)])
    with caplog.at_level("INFO", logger="real-trade-watchlist"):
        assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1
    assert [r.symbol for r in db.query(models.WatchlistEntry).all()] == ["INFY"]
    assert any("2 ETF-name item(s) ignored" in r.getMessage() for r in caplog.records)


def test_etf_names_inserted_when_retire_is_off(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_INSTRUMENT_RETIRE", "0")
    patch_source(monkeypatch, [cand("MAFANG", price=100.0)])
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_other_retirement_reasons_unaffected(db, monkeypatch):
    patch_source(monkeypatch, [cand("TESTCO", price=100.0)])
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


# ── summary logging ──────────────────────────────────────────────────────────
def test_many_adverse_rows_log_one_summary_line(db, monkeypatch, caplog):
    prices = {}
    for i in range(40):
        sym = f"STK{i:02d}"
        make_row(db, symbol=sym, catalyst_price=100.0)
        prices[sym] = 90.0                      # 10% below catalyst: held back, not retired
    patch_quotes(monkeypatch, prices)
    with caplog.at_level("INFO", logger="real-trade-entry"):
        t = evaluate(db)
    assert t["adverse"] == 40
    lines = [r.getMessage() for r in caplog.records if "SKIPPED" in r.getMessage()]
    assert len(lines) == 1 and "40 row(s)" in lines[0] and "+32 more" in lines[0]


def test_many_penny_rows_log_one_expired_line(db, monkeypatch, caplog):
    prices = {}
    for i in range(30):
        sym = f"PEN{i:02d}"
        make_row(db, symbol=sym, catalyst_price=10.0)
        prices[sym] = 10.0
    patch_quotes(monkeypatch, prices)
    with caplog.at_level("INFO", logger="real-trade-entry"):
        evaluate(db)
    assert sum("EXPIRED" in r.getMessage() for r in caplog.records) == 1


def test_adverse_summary_is_throttled_per_row(db, monkeypatch, caplog):
    make_row(db, symbol="TESTCO", catalyst_price=100.0)
    patch_quotes(monkeypatch, {"TESTCO": 90.0})
    with caplog.at_level("INFO", logger="real-trade-entry"):
        evaluate(db)
        evaluate(db)
    assert sum("SKIPPED" in r.getMessage() for r in caplog.records) == 1


def test_summary_helper_never_raises_and_ignores_empty(caplog):
    entry._log_watchlist_summary("DEMO", "SKIPPED", "x", [])
    entry._log_watchlist_summary("DEMO", "SKIPPED", "x", [None])     # malformed item: swallowed
    assert not [r for r in caplog.records if "SKIPPED" in r.getMessage()]


def test_per_row_detail_is_available_at_debug(db, monkeypatch, caplog):
    make_row(db, symbol="TESTCO", catalyst_price=100.0)
    patch_quotes(monkeypatch, {"TESTCO": 90.0})
    with caplog.at_level("DEBUG", logger="real-trade-entry"):
        evaluate(db)
    assert any(r.levelname == "DEBUG" and "TESTCO" in r.getMessage() for r in caplog.records)
