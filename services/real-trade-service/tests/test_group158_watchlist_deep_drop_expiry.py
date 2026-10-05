"""group158: a watchlist row that fell far below its catalyst is retired (status expired) and is not
re-added by the next source poll for a cooldown. Run: python3 -m pytest tests/test_group158_watchlist_deep_drop_expiry.py -q"""
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
    for k in ("WATCHLIST_ADVERSE_GUARD", "WATCHLIST_EXPIRE_DROP_PCT", "WATCHLIST_DROP_COOLDOWN_HOURS",
              "WATCHLIST_MAX_DROP_PCT", "WATCHLIST_TIER3_MIN_DAY_CHANGE_PCT", "CANDIDATE_MIN_STOCK_PRICE"):
        monkeypatch.delenv(k, raising=False)
    entry._adverse_last_log.clear()


def make_row(db, *, symbol="TESTCO", catalyst_price=100.0, source_tier=1, ctype="results"):
    row = models.WatchlistEntry(
        mode="DEMO", symbol=symbol, catalyst_type=ctype, catalyst_price=catalyst_price,
        catalyst_ts=datetime.now(timezone.utc), horizon_class="mid", decay_half_life_days=12.0,
        entry_band_pct=0.07, source_tier=source_tier, conviction_score=70.0, status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30), created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def patch_quote(monkeypatch, price, symbol="TESTCO"):
    t = Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc), atr=1.5, source="test")

    async def fake(symbols, **kw):
        return {symbol: t}
    monkeypatch.setattr(entry, "get_quotes", fake)


def test_deep_drop_expires_row_and_is_not_queued(db, monkeypatch):
    row = make_row(db)
    patch_quote(monkeypatch, 80.0)  # -20%
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1 and tally["queued"] == 0 and tally["band_ok"] == 0
    assert db.query(models.TradeCandidate).count() == 0
    db.refresh(row)
    assert row.status == "expired"
    assert row.missed_reason.startswith(entry.ADVERSE_EXPIRE_PREFIX)
    assert "-20.0%" in row.missed_reason and "100.00" in row.missed_reason


def test_moderate_drop_stays_active(db, monkeypatch):
    row = make_row(db)
    patch_quote(monkeypatch, 90.0)  # -10%: not queued (group155) but not retired either
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["adverse"] == 1
    db.refresh(row)
    assert row.status == "active"


def test_exactly_at_limit_stays_active(db, monkeypatch):
    row = make_row(db)
    patch_quote(monkeypatch, 85.0)  # -15.0%
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    db.refresh(row)
    assert row.status == "active"


def test_limit_follows_env(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_EXPIRE_DROP_PCT", "0.08")
    row = make_row(db)
    patch_quote(monkeypatch, 90.0)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    db.refresh(row)
    assert row.status == "expired"


def test_limit_zero_disables_retirement(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_EXPIRE_DROP_PCT", "0")
    row = make_row(db)
    patch_quote(monkeypatch, 50.0)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    db.refresh(row)
    assert row.status == "active"


def test_bad_env_falls_back_to_15(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_EXPIRE_DROP_PCT", "abc")
    row = make_row(db)
    patch_quote(monkeypatch, 80.0)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    db.refresh(row)
    assert row.status == "expired"


def test_guard_off_switch_keeps_old_behaviour(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_ADVERSE_GUARD", "0")
    row = make_row(db)
    patch_quote(monkeypatch, 80.0)
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["queued"] == 1
    db.refresh(row)
    assert row.status == "active"


def test_deep_drop_helper_never_raises():
    assert entry._watchlist_deep_drop_reason("x") is None
    assert entry._watchlist_deep_drop_reason(-0.5) is not None
    assert entry._watchlist_deep_drop_reason(0.2) is None


def test_a_second_pass_does_not_touch_the_retired_row(db, monkeypatch):
    row = make_row(db)
    patch_quote(monkeypatch, 80.0)
    run(entry.evaluate_watchlist_entries(db, "DEMO"))
    tally = run(entry.evaluate_watchlist_entries(db, "DEMO"))
    assert tally["watchlist_checked"] == 0  # no active rows left


# ── refresh_watchlist cooldown ──────────────────────────────────────────────

def patch_source(monkeypatch, symbol="TESTCO", ctype="results"):
    async def fake(db, mode):
        return [{"symbol": symbol, "catalyst_type": ctype, "catalyst_price": 80.0, "source_tier": 1,
                 "catalyst_ts": datetime.now(timezone.utc) + timedelta(minutes=5)}]
    monkeypatch.setattr(watchlist, "fetch_watchlist_candidates", fake)


def retire(db, row, reason="adverse: fell -20.0% below catalyst"):
    row.status = "expired"
    row.missed_reason = reason
    row.updated_at = datetime.now(timezone.utc)
    db.commit()


def test_retired_symbol_is_not_re_added_within_cooldown(db, monkeypatch):
    retire(db, make_row(db))
    patch_source(monkeypatch)
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 0
    assert db.query(models.WatchlistEntry).count() == 1


def test_re_added_after_cooldown(db, monkeypatch):
    retire(db, make_row(db))
    patch_source(monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    monkeypatch.setattr(watchlist, "_now", lambda: later)  # 30 h later, past the 24 h cooldown
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_cooldown_zero_disables(db, monkeypatch):
    monkeypatch.setenv("WATCHLIST_DROP_COOLDOWN_HOURS", "0")
    retire(db, make_row(db))
    patch_source(monkeypatch)
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_other_expired_rows_are_not_blocked(db, monkeypatch):
    # an ordinary time-expired row (no "adverse:" reason) must not block a later fresh catalyst
    row = make_row(db)
    row.status = "expired"
    db.commit()
    patch_source(monkeypatch)
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_cooldown_is_per_symbol_and_catalyst(db, monkeypatch):
    retire(db, make_row(db, symbol="OTHERCO"))
    patch_source(monkeypatch, symbol="TESTCO")
    assert run(watchlist.refresh_watchlist(db, "DEMO")) == 1


def test_cooldown_hours_env_parsing(monkeypatch):
    assert watchlist._drop_cooldown_hours() == 24.0
    for raw, want in (("6", 6.0), (" 12 ", 12.0), ("abc", 24.0), ("-3", 24.0), ("0", 0.0), ("nan", 24.0)):
        monkeypatch.setenv("WATCHLIST_DROP_COOLDOWN_HOURS", raw)
        assert watchlist._drop_cooldown_hours() == want
