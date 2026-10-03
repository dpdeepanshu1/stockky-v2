"""
tests/test_intraday_news.py

2026-10-04 (user request): lightweight MARKET-HOURS news check.
  - watchlist_engine/intraday_news.py  (scoring, dedupe, snapshot, Gate 6 reader)
  - execution/auto_pilot.py            (window, sleep cadence, tick body, loop)
  - entry_engine/entry.py              (nudge shows up in the PLACED event; see
                                        tests/test_entry_evaluate_mode_remaining_coverage_2.py)

Run from services/real-trade-service:
    python3 -m pytest tests/test_intraday_news.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import symbol_master
from execution import auto_pilot as ap
from watchlist_engine import afterhours_scan as ah
from watchlist_engine import intraday_news as inews

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


def rfc822(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    inews.reset_state_for_tests()
    monkeypatch.setattr(config, "INTRADAY_NEWS_START_IST", "09:00")
    monkeypatch.setattr(config, "INTRADAY_NEWS_END_IST", "15:45")
    monkeypatch.setattr(config, "INTRADAY_NEWS_INTERVAL_SECONDS", 900)
    monkeypatch.setattr(config, "INTRADAY_NEWS_MAX_AGE_MINUTES", 180)
    monkeypatch.setattr(config, "INTRADAY_NEWS_MIN_SCORE", 20.0)
    monkeypatch.setattr(config, "INTRADAY_NEWS_BONUS_CAP", 8.0)
    monkeypatch.setattr(config, "INTRADAY_NEWS_PENALTY_CAP", 10.0)
    monkeypatch.setattr(config, "INTRADAY_NEWS_ALERT_MIN_SCORE", 45.0)
    yield
    inews.reset_state_for_tests()


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


# ── scoring helpers ─────────────────────────────────────────────────────────────

class TestScoring:
    def test_positive_bonus_scales_and_caps(self):
        assert inews.positive_bonus_for_score(0) == 0.0
        assert inews.positive_bonus_for_score(30) == 4.0
        assert inews.positive_bonus_for_score(60) == 8.0
        assert inews.positive_bonus_for_score(100) == 8.0            # capped

    def test_negative_headline_gets_the_penalty(self):
        kind, nudge, _, _ = inews._classify("TCS shares fall after probe into accounts", 10)
        assert kind == "neg" and nudge == -10.0

    def test_cost_context_fall_is_not_negative(self):
        kind, nudge, _, _ = inews._classify("Fall in input costs lifts Asian Paints profit margin, strong results", 10)
        assert kind == "pos" and nudge > 0

    def test_weak_or_unrelated_headline_is_ignored(self):
        assert inews._classify("Reliance chairman visits temple", 10)[0] == ""


# ── merge ───────────────────────────────────────────────────────────────────────

class TestMerge:
    def _e(self, kind, bonus, minutes_ago, now):
        return {"kind": kind, "bonus": bonus, "ts": (now - timedelta(minutes=minutes_ago)).isoformat()}

    def test_expired_entries_are_dropped(self):
        now = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
        out = inews._merge({"OLD": self._e("pos", 5, 400, now)}, {}, now - timedelta(minutes=180))
        assert out == {}

    def test_same_kind_strongest_wins_and_conflicting_kind_newest_wins(self):
        now = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
        cutoff = now - timedelta(minutes=180)
        old = {"A": self._e("pos", 3, 30, now), "B": self._e("pos", 6, 60, now)}
        new = {"A": self._e("pos", 5, 5, now), "B": self._e("neg", -10, 5, now)}
        out = inews._merge(old, new, cutoff)
        assert out["A"]["bonus"] == 5
        assert out["B"]["kind"] == "neg"                                # newer bad news overrides older good news

    def test_snapshot_is_bounded(self):
        now = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
        many = {f"S{i}": self._e("pos", i % 8 + 1, 1, now) for i in range(200)}
        assert len(inews._merge({}, many, now - timedelta(minutes=180))) == 80


# ── the pass itself ─────────────────────────────────────────────────────────────

class TestRunCheck:
    def _wire(self, monkeypatch, items_by_source, known=("TCS", "INFY", "RELIANCE", "IT", "ENERGY"), sent=None):
        async def fake_fetch(feed):
            return list(items_by_source.get(feed["source"], []))

        async def fake_symbols(db_):
            return set(known)

        async def fake_notify(text):
            if sent is not None:
                sent.append(text)
            return True

        monkeypatch.setattr(ah, "_fetch_rss_items", fake_fetch)
        monkeypatch.setattr(symbol_master, "get_all_symbols", fake_symbols)
        import notifier
        monkeypatch.setattr(notifier, "notify_async", fake_notify)

    def test_stores_nudges_dedupes_and_skips_stale_undated_and_generic(self, db, monkeypatch):
        now = datetime.now(UTC)
        src = ah._RSS_FEEDS[0]["source"]
        items = {src: [
            {"title": "TCS bags large order, strong Q2 results beat estimates", "pubDate": rfc822(now - timedelta(minutes=10))},
            {"title": "INFY shares fall after probe into accounts", "pubDate": rfc822(now - timedelta(minutes=20))},
            {"title": "RELIANCE wins big contract and order", "pubDate": rfc822(now - timedelta(hours=9))},         # stale
            {"title": "TCS posts strong profit growth", "pubDate": None},                                          # undated
            {"title": "IT stocks strong results, order wins", "pubDate": rfc822(now - timedelta(minutes=5))},      # generic token
        ]}
        sent = []
        self._wire(monkeypatch, items, sent=sent)
        res = run(inews.run_intraday_news_check(db, now=now))
        assert res["ran"] and res["new"] == 3 and res["pos"] == 1 and res["neg"] == 1
        from resilience.local_cache import load_snapshot
        snap = load_snapshot(db, inews.SNAPSHOT_KEY)
        assert set(snap["symbols"]) == {"TCS", "INFY"}
        assert snap["symbols"]["TCS"]["bonus"] > 0 and snap["symbols"]["INFY"]["bonus"] == -10.0
        # second tick over the same feed: everything already seen -> nothing new, no extra write/alert
        sent.clear()
        res2 = run(inews.run_intraday_news_check(db, now=now))
        assert res2["new"] == 0 and res2["symbols"] == 0 and not sent

    def test_alert_for_queued_candidate_even_if_weak(self, db, monkeypatch):
        now = datetime.now(UTC)
        db.add(models.TradeCandidate(mode="DEMO", symbol="INFY", consumed=False))
        db.commit()
        src = ah._RSS_FEEDS[0]["source"]
        sent = []
        self._wire(monkeypatch, {src: [
            {"title": "INFY shares fall after probe into accounts", "pubDate": rfc822(now)},
        ]}, sent=sent)
        res = run(inews.run_intraday_news_check(db, now=now))
        assert res["alerted"] == 1 and "INFY" in sent[0] and "(in queue)" in sent[0] and "🔴" in sent[0]

    def test_unqueued_negative_does_not_alert(self, db, monkeypatch):
        now = datetime.now(UTC)
        src = ah._RSS_FEEDS[0]["source"]
        sent = []
        self._wire(monkeypatch, {src: [
            {"title": "INFY shares fall after probe into accounts", "pubDate": rfc822(now)},
        ]}, sent=sent)
        res = run(inews.run_intraday_news_check(db, now=now))
        assert res["symbols"] == 1 and res["alerted"] == 0 and sent == []

    def test_no_symbol_master_skips_the_pass(self, db, monkeypatch):
        self._wire(monkeypatch, {}, known=())
        res = run(inews.run_intraday_news_check(db))
        assert res["ran"] is False and res["reason"] == "symbol_master_unavailable"

    def test_feed_failure_is_harmless(self, db, monkeypatch):
        self._wire(monkeypatch, {})                                       # every feed returns []
        res = run(inews.run_intraday_news_check(db))
        assert res["ran"] is True and res["symbols"] == 0


# ── Gate 6 reader ───────────────────────────────────────────────────────────────

class TestBonusReader:
    def _save(self, db, symbols, date=None):
        from resilience.local_cache import save_snapshot
        from tz_utils import ist_today_str
        save_snapshot(db, inews.SNAPSHOT_KEY, {"date": date or ist_today_str(), "symbols": symbols})
        inews.reset_state_for_tests()

    def _entry(self, bonus, minutes_ago=5):
        return {"kind": "pos" if bonus >= 0 else "neg", "bonus": bonus,
                "ts": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()}

    def test_returns_stored_nudge(self, db):
        self._save(db, {"TCS": self._entry(6.5), "INFY": self._entry(-10.0)})
        assert inews.intraday_news_bonus(db, "TCS") == 6.5
        assert inews.intraday_news_bonus(db, "INFY") == -10.0
        assert inews.intraday_news_bonus(db, "WIPRO") == 0.0

    def test_expired_or_other_day_is_zero(self, db):
        self._save(db, {"TCS": self._entry(6.5, minutes_ago=400)})
        assert inews.intraday_news_bonus(db, "TCS") == 0.0
        self._save(db, {"TCS": self._entry(6.5)}, date="2020-01-01")
        assert inews.intraday_news_bonus(db, "TCS") == 0.0

    def test_stored_values_are_clamped_to_the_current_caps(self, db):
        self._save(db, {"TCS": self._entry(20.0), "INFY": self._entry(-50.0)})
        assert inews.intraday_news_bonus(db, "TCS") == 8.0
        assert inews.intraday_news_bonus(db, "INFY") == -10.0

    def test_never_raises(self):
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert inews.intraday_news_bonus(Boom(), "TCS") == 0.0


# ── window + cadence ────────────────────────────────────────────────────────────

def at(h, m=0, day=6):
    return datetime(2026, 10, day, h, m, tzinfo=IST)            # 2026-10-06 is a Tuesday, not a holiday


class TestWindowAndSleep:
    def test_window(self):
        assert ap._intraday_news_window_active(at(9, 0)) is True
        assert ap._intraday_news_window_active(at(12, 0)) is True
        assert ap._intraday_news_window_active(at(15, 44)) is True
        assert ap._intraday_news_window_active(at(15, 45)) is False
        assert ap._intraday_news_window_active(at(8, 59)) is False

    def test_weekend_and_holiday_are_outside(self):
        assert ap._intraday_news_window_active(at(12, 0, day=10)) is False        # Saturday
        assert ap._intraday_news_window_active(datetime(2026, 10, 20, 12, 0, tzinfo=IST)) is False   # Dussehra

    def test_in_window_sleeps_the_interval_capped_at_window_end(self):
        assert ap._intraday_news_next_sleep_seconds(at(10, 0)) == 900
        assert ap._intraday_news_next_sleep_seconds(at(15, 40)) == 300            # 15:45 end is closer

    def test_outside_window_sleeps_to_next_start(self):
        assert ap._intraday_news_next_sleep_seconds(at(7, 0)) == 2 * 3600         # -> 09:00
        assert ap._intraday_news_next_sleep_seconds(at(15, 45)) == 6 * 3600       # 17h15m away, capped at 6h
        assert ap._intraday_news_next_sleep_seconds(at(8, 59)) >= 30

    def test_never_below_30_seconds(self):
        assert ap._intraday_news_next_sleep_seconds(datetime(2026, 10, 6, 15, 44, 50, tzinfo=IST)) == 30


# ── tick body + loop ────────────────────────────────────────────────────────────

class TestTickBody:
    @pytest.fixture()
    def wired(self, monkeypatch):
        eng = create_engine("sqlite:///:memory:")
        models.Base.metadata.create_all(eng)
        factory = sessionmaker(bind=eng)
        monkeypatch.setattr(ap, "get_session_factory", lambda: factory)
        return factory

    def test_outside_window(self, wired, monkeypatch):
        monkeypatch.setattr(ap, "_intraday_news_window_active", lambda now=None: False)
        assert run(ap._intraday_news_body())["reason"] == "outside_window"

    def test_feature_disabled_when_no_gate_has_the_toggle(self, wired, monkeypatch):
        monkeypatch.setattr(ap, "_intraday_news_window_active", lambda now=None: True)
        s = wired()
        s.add(models.TradeGateState(mode="REAL", afterhours_news_scan_enabled=False))
        s.commit()
        assert run(ap._intraday_news_body())["reason"] == "feature_disabled"

    def test_runs_the_check_when_enabled(self, wired, monkeypatch):
        monkeypatch.setattr(ap, "_intraday_news_window_active", lambda now=None: True)
        s = wired()
        s.add(models.TradeGateState(mode="DEMO", afterhours_news_scan_enabled=True))
        s.commit()
        called = []

        async def fake_check(db_):
            called.append(1)
            return {"ran": True}

        monkeypatch.setattr(inews, "run_intraday_news_check", fake_check)
        assert run(ap._intraday_news_body()) == {"ran": True} and called == [1]

    def test_errors_never_propagate(self, wired, monkeypatch):
        monkeypatch.setattr(ap, "_intraday_news_window_active", lambda now=None: True)
        s = wired()
        s.add(models.TradeGateState(mode="DEMO", afterhours_news_scan_enabled=True))
        s.commit()

        async def boom(db_):
            raise RuntimeError("feed exploded")

        monkeypatch.setattr(inews, "run_intraday_news_check", boom)
        assert run(ap._intraday_news_body())["reason"] == "error"

    def test_sync_wrapper_skips_when_busy(self, monkeypatch):
        ran = []
        monkeypatch.setattr(ap, "_run_coro_in_new_loop", lambda fn, *a: ran.append(fn))
        assert ap._intraday_news_lock.acquire(blocking=False)
        try:
            ap._run_intraday_news_tick_sync()
        finally:
            ap._intraday_news_lock.release()
        assert ran == []
        ap._run_intraday_news_tick_sync()
        assert ran == [ap._intraday_news_body]


def test_loop_sleeps_by_window(monkeypatch):
    sleeps = []

    class _Stop(Exception):
        pass

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise _Stop

    async def fake_to_thread(fn, *a, **kw):
        return None

    monkeypatch.setattr(ap.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ap.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(ap, "_intraday_news_next_sleep_seconds", lambda now=None: 777.0)
    with pytest.raises(_Stop):
        run(ap._intraday_news_loop())
    assert sleeps[-1] == 777.0
