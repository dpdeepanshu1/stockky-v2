"""group294: a trade that closes AFTER the end-of-day report was built is picked up by the refresh, even when every
trade already in the snapshot has final (ledger) charges."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import trade_records as tr
from execution import auto_pilot as ap
from test_group290_trade_records import seed_trade
from tz_utils import ist_today_str


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def final_snap(built, closed_count, day="2026-10-09"):
    return {"day": day, "generated_at": built, "closed_count": closed_count,
            "records_today": [{"charges_source": "ledger"}]}


@pytest.fixture
def due(monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 15.0)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_UNTIL_IST", "18:00")
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: t.hour < 18)
    seen = []
    monkeypatch.setattr(tr, "closed_count_for_day", lambda db, mode, day: seen.append((mode, day)) or 2)
    return seen


def test_due_when_more_positions_closed_than_the_snapshot_knows(due):
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), object(), "REAL") is True
    assert due == [("REAL", "2026-10-09")]


def test_not_due_when_the_count_is_the_same(due):
    assert ap._daily_report_refresh_due(final_snap(ago(20), 2), object(), "REAL") is False


def test_not_due_inside_the_interval_and_the_count_is_not_even_read(due):
    assert ap._daily_report_refresh_due(final_snap(ago(5), 1), object(), "REAL") is False
    assert due == []


def test_not_due_after_the_cutoff_or_when_off(due, monkeypatch):
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: True)
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), object(), "REAL") is False
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: t.hour < 18)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 0.0)
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), object(), "REAL") is False


def test_a_snapshot_without_a_count_or_an_unreadable_count_never_triggers(due, monkeypatch):
    old = final_snap(ago(20), None)
    assert ap._daily_report_refresh_due(old, object(), "REAL") is False
    monkeypatch.setattr(tr, "closed_count_for_day", lambda *a: None)
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), object(), "REAL") is False


def test_without_a_db_the_old_behaviour_is_unchanged(due):
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1)) is False
    assert due == []


def test_pending_charges_still_trigger_without_reading_the_count(due):
    snap = final_snap(ago(20), 2)
    snap["records_today"] = [{"charges_source": "estimate"}]
    assert ap._daily_report_refresh_due(snap, object(), "REAL") is True
    assert due == []


def test_count_failure_inside_due_never_raises(due, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(tr, "closed_count_for_day", boom)
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), object(), "REAL") is False


def test_closed_count_returns_none_on_a_broken_db():
    assert tr.closed_count_for_day(object(), "REAL", "2026-10-09") is None
    assert tr.closed_count_for_day(object(), "REAL", "not-a-day") is None


# ── real SQLite, through the schedule tick ──────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def tick(engine, monkeypatch):
    factory = sessionmaker(bind=engine)
    pushed = []

    async def notify(text):
        pushed.append(text)
        return True
    monkeypatch.setattr(ap, "get_session_factory", lambda: factory)
    monkeypatch.setattr(ap, "notify_async", notify)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", True, raising=False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "00:00", raising=False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 15.0)
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: True)
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: t.hour < 18)
    monkeypatch.setattr(ap, "is_market_open_ist", lambda *a, **k: False)
    monkeypatch.setattr(ap, "_edis_check_enabled", lambda *a, **k: False)
    ap._daily_report_last_failure.clear()
    yield {"factory": factory, "pushed": pushed}
    ap._daily_report_last_failure.clear()


def _age_stored(factory, minutes):
    from resilience import local_cache
    s = factory()
    try:
        key = tr.snapshot_key("REAL", ist_today_str())
        snap = dict(local_cache.load_snapshot(s, key), generated_at=ago(minutes))
        local_cache.save_snapshot(s, key, snap)
    finally:
        s.close()


def test_a_trade_closed_after_the_report_is_added_by_the_next_refresh(tick):
    s = tick["factory"]()
    try:
        seed_trade(s, "AAA", mode="REAL", opened_min=60, closed_min=20)          # ledger booked: charges final
    finally:
        s.close()
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        first = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert tr.charges_pending(first) == 0 and first["closed_count"] == 1
    assert len(tick["pushed"]) == 1

    s = tick["factory"]()
    try:
        seed_trade(s, "BBB", mode="REAL", opened_min=30, closed_min=1)           # a manual exit after the report
    finally:
        s.close()
    asyncio.run(ap._schedule_tick_body("REAL"))                                    # too soon: nothing
    assert len(tick["pushed"]) == 1

    _age_stored(tick["factory"], 20)
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        final = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert final["closed_count"] == 2 and final["today"]["overall"]["trades"] == 2 and final["refresh_count"] == 1
    assert len(tick["pushed"]) == 2 and "(updated)" in tick["pushed"][1] and "2 trades" in tick["pushed"][1]

    _age_stored(tick["factory"], 20)                                               # nothing new: no third message
    asyncio.run(ap._schedule_tick_body("REAL"))
    assert len(tick["pushed"]) == 2


def test_without_a_db_even_with_a_mode_nothing_is_read(due):
    assert ap._daily_report_refresh_due(final_snap(ago(20), 1), None, "REAL") is False
    assert due == []


def test_closed_count_counts_only_closed_positions_of_that_mode_and_ist_day(engine):
    s = sessionmaker(bind=engine)()
    try:
        seed_trade(s, "AAA", mode="REAL", opened_min=30, closed_min=1)
        seed_trade(s, "BBB", mode="DEMO", opened_min=30, closed_min=1)
        seed_trade(s, "CCC", mode="REAL", opened_min=3000, closed_min=2900)         # two days ago
        p = seed_trade(s, "DDD", mode="REAL", opened_min=30, closed_min=1)
        p.status = "OPEN"
        f = seed_trade(s, "EEE", mode="REAL", opened_min=30, closed_min=1)
        f.closed_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1, hours=1)   # next IST day
        s.commit()
        today = ist_today_str()
        assert tr.closed_count_for_day(s, "REAL", today) == 1
        assert tr.closed_count_for_day(s, "DEMO", today) == 1
        nxt = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        assert tr.closed_count_for_day(s, "REAL", nxt) == 1
    finally:
        s.close()
