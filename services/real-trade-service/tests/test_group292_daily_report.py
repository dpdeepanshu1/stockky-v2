"""group292: end-of-day expectancy report - stored snapshot + one Telegram summary per trading day.

This file: the pure parts (snapshot building, message text) and the scheduler step with every collaborator faked.
The real SQLite round trip and the HTTP routes are in test_group292_daily_report_db.py."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import trade_records as tr
from execution import auto_pilot as ap


def rec(day="2026-10-09", *, pnl=50.0, net=None, charges=None, reason="target_hit", hour="10:00", tier=1,
        slip=None, held=20.0, **kw):
    d = {"symbol": "AAA", "exit_day_ist": day, "gross_pnl": pnl, "net_pnl": net, "charges": charges,
         "exit_reason": reason, "entry_hour_ist": hour, "tier_name": f"tier{tier}", "catalyst_type": "results",
         "source_tab": "watchlist", "entry_slippage_pct": slip, "held_minutes": held}
    d.update(kw)
    return d


def run(coro):
    return asyncio.run(coro)


# ── keys and day filter ────────────────────────────────────────────────────────────────────────────────────────────

def test_snapshot_key_is_mode_and_day():
    assert tr.snapshot_key("REAL", "2026-10-09") == "daily_report:REAL:2026-10-09"
    assert tr.snapshot_key("DEMO", "2026-10-09") != tr.snapshot_key("REAL", "2026-10-09")


def test_records_for_day_keeps_only_that_ist_day():
    rows = [rec("2026-10-09"), rec("2026-10-08"), rec(None), rec("2026-10-09", reason="stop_loss")]
    got = tr.records_for_day(rows, "2026-10-09")
    assert len(got) == 2 and all(r["exit_day_ist"] == "2026-10-09" for r in got)
    assert tr.records_for_day([], "2026-10-09") == []


# ── build_daily_snapshot ───────────────────────────────────────────────────────────────────────────────────────────

def test_snapshot_splits_today_from_the_rolling_week(monkeypatch):
    seen = {}

    def fake_load(db, mode, days, symbol=None):
        seen.update(mode=mode, days=days)
        return [rec("2026-10-09", pnl=30.0), rec("2026-10-09", pnl=-10.0), rec("2026-10-08", pnl=100.0)]
    monkeypatch.setattr(tr, "load_records", fake_load)
    snap = tr.build_daily_snapshot(object(), "REAL", "2026-10-09")
    assert seen == {"mode": "REAL", "days": 7}
    assert snap["mode"] == "REAL" and snap["day"] == "2026-10-09" and snap["week_days"] == 7
    assert snap["today"]["overall"]["trades"] == 2 and snap["today"]["overall"]["gross_pnl"] == 20.0
    assert snap["week"]["overall"]["trades"] == 3 and snap["week"]["overall"]["gross_pnl"] == 120.0
    assert len(snap["records_today"]) == 2 and snap["generated_at"]


def test_snapshot_with_no_trades_has_an_empty_today(monkeypatch):
    monkeypatch.setattr(tr, "load_records", lambda *a, **k: [])
    snap = tr.build_daily_snapshot(object(), "DEMO", "2026-10-09")
    assert snap["today"]["overall"] is None and snap["week"]["overall"] is None and snap["records_today"] == []


@pytest.mark.parametrize("asked, used", [(0, 2), (1, 2), (7, 7), (60, 60), (9999, 60)])
def test_week_days_is_clamped(monkeypatch, asked, used):
    seen = {}

    def fake_load(db, mode, days, symbol=None):
        seen["d"] = days
        return []
    monkeypatch.setattr(tr, "load_records", fake_load)
    snap = tr.build_daily_snapshot(object(), "REAL", "2026-10-09", week_days=asked)
    assert seen["d"] == used and snap["week_days"] == used


def test_snapshot_day_defaults_to_today_ist(monkeypatch):
    import tz_utils
    monkeypatch.setattr(tz_utils, "ist_today_str", lambda *a, **k: "2031-01-02")
    monkeypatch.setattr(tr, "load_records", lambda *a, **k: [])
    assert tr.build_daily_snapshot(object(), "REAL")["day"] == "2031-01-02"


def test_a_database_failure_is_raised_to_the_caller(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(tr, "load_records", boom)
    with pytest.raises(RuntimeError):
        tr.build_daily_snapshot(object(), "REAL", "2026-10-09")


# ── message text ───────────────────────────────────────────────────────────────────────────────────────────────────

def snap_of(records, week=None, day="2026-10-09"):
    return {"mode": "REAL", "day": day, "week_days": 7, "today": tr.expectancy_report(records),
            "week": tr.expectancy_report(week if week is not None else records)}


def test_message_for_a_day_without_trades():
    msg = tr.format_daily_message(snap_of([]))
    assert "REAL 2026-10-09" in msg and msg.endswith("No closed trades today.")
    assert "\U0001F4CA" in msg and "\u2014" in msg and "\\u" not in msg


def test_message_for_an_empty_snapshot_dict():
    assert "No closed trades today." in tr.format_daily_message({})


def test_message_numbers():
    rows = [rec(pnl=60.0, net=50.0, charges=10.0, reason="target_hit", slip=0.2),
            rec(pnl=-30.0, net=-40.0, charges=10.0, reason="stop_loss", slip=0.4),
            rec(pnl=20.0, net=10.0, charges=10.0, reason="target_hit")]
    msg = tr.format_daily_message(snap_of(rows))
    assert "3 trades, 2 wins / 1 losses (67%)" in msg
    assert "Gross \u20b9+50.00, charges \u20b930.00, net \u20b9+20.00" in msg
    assert "Expectancy \u20b9+6.67 per trade, payoff 0.75" in msg           # avg win 30 / avg loss 40
    assert "Avg entry slippage vs signal +0.30%" in msg
    assert "\\n" not in msg and len(msg.splitlines()) >= 6


def test_message_hides_unknowns_instead_of_printing_zero():
    rows = [rec(pnl=10.0, net=None, charges=None), rec(pnl=20.0, net=None, charges=None)]
    msg = tr.format_daily_message(snap_of(rows))
    assert "charges \u20b9n/a" in msg and "net \u20b9n/a" in msg
    assert "payoff" not in msg                                              # no losing trade -> no ratio
    assert "slippage" not in msg                                            # no slippage known


def test_message_lists_the_worst_exit_reason_first_and_at_most_six():
    rows = []
    for i, reason in enumerate(["a", "b", "c", "d", "e", "f", "g", "h"]):
        v = 10.0 * (i + 1) * (-1 if reason == "c" else 1)
        rows.append(rec(pnl=v, net=v, reason=reason))
    line = [ln for ln in tr.format_daily_message(snap_of(rows)).splitlines() if ln.startswith("By exit")][0]
    assert line.index(" c ") < line.index(" a ")                            # the loser leads
    assert line.count("x \u20b9") == 6


def test_message_week_line_and_noise_note():
    today = [rec("2026-10-09", pnl=5.0)]
    week = today + [rec("2026-10-08", pnl=15.0) for _ in range(6)]
    msg = tr.format_daily_message(snap_of(today, week))
    assert "Last 7 days: 7 trades, expectancy \u20b9+13.57, win rate 100%" in msg
    assert msg.endswith("(few trades today - treat as noise)")


def test_message_no_noise_note_with_enough_trades():
    rows = [rec(pnl=5.0) for _ in range(5)]
    assert "treat as noise" not in tr.format_daily_message(snap_of(rows))


def test_message_without_a_week_block():
    s = snap_of([rec(pnl=5.0)])
    s["week"] = None
    assert "Last 7 days" not in tr.format_daily_message(s)


# ── store / load through the cache helper (faked) ─────────────────────────────────────────────────────────────────

@pytest.fixture
def cache(monkeypatch):
    from resilience import local_cache
    store = {"data": {}, "writes": 0, "lose_writes": False}

    def save(db, key, payload):
        store["writes"] += 1
        if not store["lose_writes"]:
            store["data"][key] = payload

    def load(db, key):
        return store["data"].get(key)
    monkeypatch.setattr(local_cache, "save_snapshot", save)
    monkeypatch.setattr(local_cache, "load_snapshot", load)
    return store


def test_save_then_load_round_trip(cache):
    snap = {"mode": "REAL", "day": "2026-10-09", "x": 1}
    assert tr.save_daily_snapshot(object(), snap) is True
    assert tr.load_daily_snapshot(object(), "REAL", "2026-10-09") == snap
    assert tr.load_daily_snapshot(object(), "REAL", "2026-10-10") is None
    assert tr.load_daily_snapshot(object(), "DEMO", "2026-10-09") is None


def test_save_reports_false_when_the_write_was_swallowed(cache):
    cache["lose_writes"] = True
    assert tr.save_daily_snapshot(object(), {"mode": "REAL", "day": "2026-10-09"}) is False


# ── the scheduler step ─────────────────────────────────────────────────────────────────────────────────────────────

class FakeDb:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def step(monkeypatch, cache):
    """A weekday, 15:50 IST, report on, nothing stored yet, one closed trade today. Everything observable in `st`."""
    st = {"pushed": [], "builds": 0, "notify_result": True, "now": 1000.0, "snaps": []}
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", True, raising=False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "15:45", raising=False)
    monkeypatch.setattr(ap, "ist_today_str", lambda *a, **k: "2026-10-09")
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: True)
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: (t.hour, t.minute) <= (15, 50))
    ap._daily_report_last_failure.clear()

    def build(db, mode, day=None, **k):
        st["builds"] += 1
        if st.get("build_error"):
            raise st["build_error"]
        recs = st.get("records", [rec(day or "2026-10-09", pnl=40.0, net=30.0, charges=10.0)])
        return {"mode": mode, "day": day, "week_days": 7, "generated_at": "t",
                "today": tr.expectancy_report(recs), "week": tr.expectancy_report(recs), "records_today": recs}
    monkeypatch.setattr(tr, "build_daily_snapshot", build)

    async def notify(text):
        st["pushed"].append(text)
        if st.get("notify_error"):
            raise st["notify_error"]
        return st["notify_result"]
    monkeypatch.setattr(ap, "notify_async", notify)
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: st["now"])
    st["cache"] = cache
    yield st
    ap._daily_report_last_failure.clear()


def go(db=None, mode="REAL", **kw):
    return run(ap._daily_report_step(db or FakeDb(), mode, **kw))


def test_first_run_stores_and_pushes_once(step):
    snap = go()
    assert snap["day"] == "2026-10-09" and snap["today"]["overall"]["trades"] == 1
    assert len(step["pushed"]) == 1 and "REAL 2026-10-09" in step["pushed"][0] and "1 trades" in step["pushed"][0]
    assert "daily_report:REAL:2026-10-09" in step["cache"]["data"]


def test_the_stored_snapshot_is_the_once_a_day_guard(step):
    assert go() is not None
    assert go() is None and go() is None
    assert len(step["pushed"]) == 1 and step["builds"] == 1


def test_modes_are_independent(step):
    assert go(mode="DEMO") is not None and go(mode="REAL") is not None
    assert len(step["pushed"]) == 2 and "DEMO" in step["pushed"][0] and "REAL" in step["pushed"][1]


def test_a_new_day_runs_again(step, monkeypatch):
    assert go() is not None
    monkeypatch.setattr(ap, "ist_today_str", lambda *a, **k: "2026-10-12")
    assert go() is not None
    assert len(step["pushed"]) == 2


def test_disabled_does_nothing(step, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", False)
    assert go() is None and step["builds"] == 0 and step["pushed"] == []


def test_before_the_report_time_does_nothing(step, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "16:30")
    assert go() is None and step["builds"] == 0


def test_the_time_is_configurable(step, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "15:50")
    assert go() is not None


def test_an_unreadable_time_falls_back_to_1545(step, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "nonsense")
    assert go() is not None                                                  # fake clock is 15:50 >= 15:45


def test_weekend_does_nothing(step, monkeypatch):
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: False)
    assert go() is None and step["builds"] == 0


def test_no_closed_trades_stores_but_does_not_push(step):
    step["records"] = []
    snap = go()
    assert snap is not None and snap["today"]["overall"] is None
    assert step["pushed"] == [] and "daily_report:REAL:2026-10-09" in step["cache"]["data"]
    assert go() is None                                                      # and it is not rebuilt every tick


def test_a_failed_store_is_not_pushed_and_retries_after_ten_minutes(step):
    step["cache"]["lose_writes"] = True
    db = FakeDb()
    assert go(db) is None
    assert step["pushed"] == [] and db.rollbacks == 1 and step["builds"] == 1
    step["now"] += 599.0
    assert go(db) is None and step["builds"] == 1                            # throttled
    step["now"] += 2.0
    step["cache"]["lose_writes"] = False
    assert go(db) is not None and step["builds"] == 2                        # retried, stored, pushed
    assert len(step["pushed"]) == 1
    assert ap._daily_report_last_failure == {}


def test_a_build_error_is_throttled_the_same_way(step):
    step["build_error"] = RuntimeError("db down")
    assert go() is None and go() is None
    assert step["builds"] == 1 and step["pushed"] == []
    step["now"] += 601.0
    step["build_error"] = None
    assert go() is not None and step["builds"] == 2


def test_failure_throttle_is_per_mode(step):
    step["build_error"] = RuntimeError("db down")
    assert go(mode="REAL") is None
    step["build_error"] = None
    assert go(mode="DEMO") is not None


def test_rollback_failure_does_not_escape(step):
    class BadDb(FakeDb):
        def rollback(self):
            raise RuntimeError("rollback failed")
    step["build_error"] = RuntimeError("x")
    assert go(BadDb()) is None


def test_undelivered_push_keeps_the_stored_snapshot_and_does_not_resend(step):
    step["notify_result"] = False
    assert go() is not None and len(step["pushed"]) == 1
    assert go() is None and len(step["pushed"]) == 1


def test_a_raising_push_still_returns_the_snapshot(step):
    step["notify_error"] = RuntimeError("telegram down")
    assert go() is not None and "daily_report:REAL:2026-10-09" in step["cache"]["data"]


def test_a_broken_snapshot_read_never_raises(step, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("read failed")
    monkeypatch.setattr(tr, "load_daily_snapshot", boom)
    assert go() is None and step["pushed"] == []


def test_force_ignores_switch_time_weekend_and_stored_snapshot(step, monkeypatch):
    assert go() is not None
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", False)
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "23:59")
    step["records"] = [rec(pnl=-5.0, net=-6.0, charges=1.0, reason="stop_loss")]
    snap = go(force=True)
    assert snap["today"]["overall"]["net_pnl"] == -6.0
    assert len(step["pushed"]) == 2
    assert step["cache"]["data"]["daily_report:REAL:2026-10-09"]["today"]["overall"]["net_pnl"] == -6.0   # overwritten


def test_force_pushes_even_without_trades(step):
    step["records"] = []
    assert go(force=True) is not None
    assert len(step["pushed"]) == 1 and "No closed trades today." in step["pushed"][0]


def test_force_with_a_failing_store_returns_none(step):
    step["cache"]["lose_writes"] = True
    assert go(force=True) is None and step["pushed"] == []


def test_force_ignores_the_failure_throttle(step):
    step["build_error"] = RuntimeError("x")
    assert go() is None
    step["build_error"] = None
    assert go(force=True) is not None


# ── config ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_config_defaults_and_overrides(monkeypatch):
    import importlib
    import config
    monkeypatch.delenv("DAILY_REPORT_ENABLED", raising=False)
    monkeypatch.delenv("DAILY_REPORT_TIME_IST", raising=False)
    saved = dict(vars(config))
    try:
        importlib.reload(config)
        assert config.DAILY_REPORT_ENABLED is True and config.DAILY_REPORT_TIME_IST == "15:45"
        for off in ("0", "false", "No", " OFF "):
            monkeypatch.setenv("DAILY_REPORT_ENABLED", off)
            importlib.reload(config)
            assert config.DAILY_REPORT_ENABLED is False, off
        monkeypatch.setenv("DAILY_REPORT_ENABLED", "1")
        monkeypatch.setenv("DAILY_REPORT_TIME_IST", "16:10")
        importlib.reload(config)
        assert config.DAILY_REPORT_ENABLED is True and config.DAILY_REPORT_TIME_IST == "16:10"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        for k, v in saved.items():
            if not k.startswith("__"):
                setattr(config, k, v)
