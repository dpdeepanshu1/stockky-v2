"""
tests/test_overnight_holds.py — automated coverage for
execution/auto_pilot.py's _select_overnight_holds (2026-09-18 follow-on
item #10: "No automated tests for cost_model.py or _select_overnight_holds
— verified by code review + compile checks only").

Same functional-SQLite-DB pattern this codebase already uses elsewhere
(see e.g. session38/47/60's delivered zips) — an in-memory SQLite engine +
the real models.py schema, with only the two things _select_overnight_holds
itself can't get from a DB (live quotes, and reading a live account row)
monkeypatched to fixture data. Nothing about _select_overnight_holds' own
decision logic is touched or reimplemented.

Run from services/real-trade-service:
    python tests/test_overnight_holds.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from market_feed.feed import Tick
import market_feed.feed as feed_module
import portfolio.portfolio as portfolio_module
from execution.auto_pilot import _select_overnight_holds

_engine = create_engine("sqlite:///:memory:")
_Session = sessionmaker(bind=_engine)


def _fresh_db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    return _Session()


def _make_position(db, symbol, entry_price, qty, label, conviction, opened_at=None):
    p = models.TradePosition(
        mode="REAL", symbol=symbol, status="OPEN", qty_open=qty,
        avg_entry_price=entry_price, opened_at=opened_at or datetime.now(timezone.utc),
        entry_decision_label=label, entry_conviction_score=conviction,
    )
    db.add(p)
    db.flush()
    return p


def _make_account(db, equity=100_000.0):
    acct = models.TradeAccount(mode="REAL", starting_capital=equity,
                                current_equity=equity, cash_available=equity)
    db.add(acct)
    db.flush()
    return acct


def _patch_quotes(monkeypatch_ticks: dict):
    async def _fake_get_quotes(symbols):
        return {s: t for s, t in monkeypatch_ticks.items() if s in symbols}
    feed_module.get_quotes = _fake_get_quotes


def _tick(symbol, price, day_high, day_low):
    return Tick(symbol=symbol, price=price, as_of=datetime.now(timezone.utc),
                atr=None, source="test", day_high=day_high, day_low=day_low)


def _run(coro):
    return asyncio.run(coro)


def test_ineligible_label_never_selected():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "FOO", 100.0, 10, "VOLUME_SHOCK", 60)
    _patch_quotes({"FOO": _tick("FOO", 105.0, 106.0, 99.0)})
    keep_ids, reasons = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == set()
    assert reasons == {}


def test_eligible_profitable_mid_range_is_selected():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "BAR", 100.0, 10, "VOLUME_SHOCK_HIGH_CONVICTION", 70)
    # LTP 105, day range 95-110 -> range_pos = (105-95)/(110-95) = 0.667, below the 0.80 cap
    _patch_quotes({"BAR": _tick("BAR", 105.0, 110.0, 95.0)})
    keep_ids, reasons = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == {p.id}
    assert p.id in reasons
    assert "VOLUME_SHOCK_HIGH_CONVICTION" in reasons[p.id]


def test_losing_position_excluded_when_profitability_required():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "BAZ", 100.0, 10, "VOLUME_SHOCK_UPPER_CIRCUIT", 80)
    _patch_quotes({"BAZ": _tick("BAZ", 95.0, 100.0, 90.0)})  # below entry
    keep_ids, _ = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == set()


def test_extended_near_high_excluded():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "QUX", 100.0, 10, "VOLUME_SHOCK_UPPER_CIRCUIT", 80)
    # LTP 109, day range 90-110 -> range_pos = 19/20 = 0.95, above the 0.80 cap
    _patch_quotes({"QUX": _tick("QUX", 109.0, 110.0, 90.0)})
    keep_ids, _ = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == set()


def test_missing_range_data_fails_closed():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "ZAP", 100.0, 10, "VOLUME_SHOCK_UPPER_CIRCUIT", 80)
    _patch_quotes({"ZAP": _tick("ZAP", 105.0, None, None)})  # no day_high/day_low
    keep_ids, _ = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == set()


def test_missing_quote_fails_closed():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "NOPE", 100.0, 10, "VOLUME_SHOCK_UPPER_CIRCUIT", 80)
    _patch_quotes({})  # no tick for NOPE at all
    keep_ids, _ = _run(_select_overnight_holds(db, "REAL", [p]))
    assert keep_ids == set()


def test_exposure_cap_keeps_highest_conviction_first():
    # Session 72: rewritten. The original scenario (two positions worth 2,100 each on equity 10,000) predates
    # OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT (15% -> 1,500 per symbol), so BOTH were now (correctly) rejected by the
    # single-symbol cap and the test failed on the untouched v7 zip. With single<=15% and total<=40% the total cap
    # only binds with 3+ positions: three worth 1,365 each (4,095 > 4,000) -> the two highest-conviction fit.
    db = _fresh_db()
    _make_account(db, equity=10_000.0)  # total cap = 40% * 10,000 = 4,000; single-symbol cap = 1,500
    p_low = _make_position(db, "LOWCONV", 100.0, 13, "VOLUME_SHOCK_HIGH_CONVICTION", 40)
    p_mid = _make_position(db, "MIDCONV", 100.0, 13, "VOLUME_SHOCK_HIGH_CONVICTION", 65)
    p_high = _make_position(db, "HIGHCONV", 100.0, 13, "VOLUME_SHOCK_HIGH_CONVICTION", 90)
    _patch_quotes({
        "LOWCONV": _tick("LOWCONV", 105.0, 108.0, 95.0),
        "MIDCONV": _tick("MIDCONV", 105.0, 108.0, 95.0),
        "HIGHCONV": _tick("HIGHCONV", 105.0, 108.0, 95.0),
    })
    keep_ids, reasons = _run(_select_overnight_holds(db, "REAL", [p_low, p_mid, p_high]))
    assert keep_ids == {p_high.id, p_mid.id}
    assert p_low.id not in reasons


def test_feature_disabled_returns_nothing():
    db = _fresh_db()
    _make_account(db)
    p = _make_position(db, "OFF", 100.0, 10, "VOLUME_SHOCK_UPPER_CIRCUIT", 80)
    _patch_quotes({"OFF": _tick("OFF", 105.0, 110.0, 95.0)})
    original = config.OVERNIGHT_HOLD_ENABLED
    config.OVERNIGHT_HOLD_ENABLED = False
    try:
        keep_ids, reasons = _run(_select_overnight_holds(db, "REAL", [p]))
    finally:
        config.OVERNIGHT_HOLD_ENABLED = original
    assert keep_ids == set()
    assert reasons == {}


def test_empty_positions_list_short_circuits():
    db = _fresh_db()
    _make_account(db)
    keep_ids, reasons = _run(_select_overnight_holds(db, "REAL", []))
    assert keep_ids == set()
    assert reasons == {}


_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failures}/{len(_TESTS)} passed")
    if failures:
        sys.exit(1)
