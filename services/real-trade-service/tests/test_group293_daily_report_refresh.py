"""group293: the stored end-of-day snapshot is rebuilt while some trade still has ESTIMATED charges (the ledger had not booked
every order at 15:45), until DAILY_REPORT_REFRESH_UNTIL_IST; one "updated" message only when the figures changed."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import trade_records as tr
from execution import auto_pilot as ap
# the fixtures and helpers of the group 292 file are reused as they are
from test_group292_daily_report import FakeDb, cache, go, rec, run, snap_of, step  # noqa: F401

KEY = "daily_report:REAL:2026-10-09"


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def refresh(step, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 15.0)
    step["generated_at"] = ago(0)
    return step


def est(charges=10.0, net=30.0, source="estimate"):
    return rec(pnl=40.0, net=net, charges=charges, charges_source=source)


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_charges_pending_counts_every_source_but_ledger():
    s = {"records_today": [rec(charges_source="ledger"), rec(charges_source="ledger_estimated"),
                           rec(charges_source="estimate"), rec(charges_source=None), rec()]}
    assert tr.charges_pending(s) == 4


@pytest.mark.parametrize("s", [None, {}, {"records_today": None}, {"records_today": []}])
def test_charges_pending_is_zero_without_records(s):
    assert tr.charges_pending(s) == 0


def test_snapshot_figures():
    s = snap_of([rec(pnl=60.0, net=50.0, charges=10.0)])
    assert tr.snapshot_figures(s) == (1, 10.0, 50.0)
    assert tr.snapshot_figures(None) == (0, None, None) == tr.snapshot_figures({"today": {"overall": None}})


# ── message ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_message_says_how_many_trades_are_still_estimated():
    s = snap_of([est(), est(source="ledger")])
    s["records_today"] = [est(), est(source="ledger")]
    assert "(charges still estimated for 1 of 2 trades)" in tr.format_daily_message(s)


def test_message_has_no_estimate_note_when_final():
    s = snap_of([est(source="ledger")])
    s["records_today"] = [est(source="ledger")]
    assert "still estimated" not in tr.format_daily_message(s)


def test_updated_header_only_when_asked():
    s = snap_of([est()])
    assert "Daily trade report (updated) \u2014 REAL 2026-10-09" in tr.format_daily_message(s, updated=True)
    assert "(updated)" not in tr.format_daily_message(s)


# ── _daily_report_refresh_due ──────────────────────────────────────────────────────────────────────────────────────

def pending_snap(built):
    return {"generated_at": built, "records_today": [est()]}


def test_due_when_pending_old_enough_and_before_the_cutoff(refresh):
    assert ap._daily_report_refresh_due(pending_snap(ago(16))) is True


def test_not_due_when_built_recently(refresh):
    assert ap._daily_report_refresh_due(pending_snap(ago(14))) is False


def test_not_due_when_final(refresh):
    assert ap._daily_report_refresh_due({"generated_at": ago(99), "records_today": [est(source="ledger")]}) is False


def test_not_due_when_switched_off(refresh, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 0.0)
    assert ap._daily_report_refresh_due(pending_snap(ago(99))) is False


def test_not_due_after_the_cutoff(refresh, monkeypatch):
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: True)
    assert ap._daily_report_refresh_due(pending_snap(ago(99))) is False


def test_the_cutoff_is_configurable_and_unreadable_falls_back_to_1800(refresh, monkeypatch):
    seen = []
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: seen.append((t.hour, t.minute)) or False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_UNTIL_IST", "17:20")
    ap._daily_report_refresh_due(pending_snap(ago(99)))
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_UNTIL_IST", "nonsense")
    ap._daily_report_refresh_due(pending_snap(ago(99)))
    assert seen == [(17, 20), (18, 0)]


@pytest.mark.parametrize("built", [None, "garbage", 5, ""])
def test_an_unreadable_build_time_counts_as_old(refresh, built):
    assert ap._daily_report_refresh_due(pending_snap(built)) is True


def test_a_naive_build_time_is_read_as_utc(refresh):
    naive = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert ap._daily_report_refresh_due(pending_snap(naive)) is True


def test_refresh_due_never_raises(refresh, monkeypatch):
    monkeypatch.setattr(ap.trade_records, "charges_pending", lambda s: 1 / 0)
    assert ap._daily_report_refresh_due(pending_snap(ago(99))) is False


# ── the step ───────────────────────────────────────────────────────────────────────────────────────────────────────

def age_stored(step, minutes):
    step["cache"]["data"][KEY]["generated_at"] = ago(minutes)


def test_first_run_says_charges_are_estimated(refresh):
    refresh["records"] = [est()]
    assert go() is not None
    assert "still estimated for 1 of 1 trades" in refresh["pushed"][0] and "(updated)" not in refresh["pushed"][0]


def test_no_refresh_inside_the_interval(refresh):
    refresh["records"] = [est()]
    go()
    assert go() is None and refresh["builds"] == 1


def test_refresh_after_the_interval_overwrites_and_pushes_an_update_when_figures_changed(refresh):
    refresh["records"] = [est(charges=10.0, net=30.0)]
    go()
    age_stored(refresh, 20)
    refresh["records"] = [est(charges=14.0, net=26.0, source="ledger")]
    snap = go()
    assert snap is not None and refresh["builds"] == 2 and snap["refresh_count"] == 1 and snap["refreshed_at"]
    assert len(refresh["pushed"]) == 2 and "(updated)" in refresh["pushed"][1]
    assert "still estimated" not in refresh["pushed"][1]
    stored = refresh["cache"]["data"][KEY]
    assert stored["today"]["overall"]["net_pnl"] == 26.0 and stored["refresh_count"] == 1


def test_a_final_snapshot_is_not_refreshed_again(refresh):
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 20)
    refresh["records"] = [est(charges=14.0, net=26.0, source="ledger")]
    go()
    age_stored(refresh, 99)
    assert go() is None and refresh["builds"] == 2


def test_unchanged_figures_store_but_do_not_push(refresh):
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 20)
    snap = go()
    assert snap is not None and snap["refresh_count"] == 1 and len(refresh["pushed"]) == 1
    assert refresh["cache"]["data"][KEY]["refresh_count"] == 1


def test_refresh_count_keeps_growing(refresh):
    refresh["records"] = [est()]
    go()
    for n in (1, 2, 3):
        age_stored(refresh, 20)
        assert go()["refresh_count"] == n


def test_a_new_trade_in_the_refresh_also_triggers_the_update(refresh):
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 20)
    refresh["records"] = [est(), est(charges=5.0, net=35.0)]
    go()
    assert len(refresh["pushed"]) == 2 and "2 trades" in refresh["pushed"][1]


def test_a_failed_refresh_keeps_the_stored_snapshot_and_is_throttled(refresh):
    refresh["records"] = [est()]
    go()
    before = dict(refresh["cache"]["data"][KEY])
    age_stored(refresh, 20)
    aged = refresh["cache"]["data"][KEY]["generated_at"]
    refresh["build_error"] = RuntimeError("db down")
    assert go() is None and go() is None
    assert refresh["builds"] == 2                                            # first + exactly one failed refresh
    assert refresh["cache"]["data"][KEY]["generated_at"] == aged and len(refresh["pushed"]) == 1
    assert before["today"] == refresh["cache"]["data"][KEY]["today"]
    refresh["build_error"] = None
    refresh["now"] += 601.0
    assert go() is not None and refresh["builds"] == 3


def test_a_refresh_that_cannot_be_stored_pushes_nothing(refresh):
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 20)
    refresh["cache"]["lose_writes"] = True
    refresh["records"] = [est(charges=14.0, net=26.0, source="ledger")]
    assert go() is None and len(refresh["pushed"]) == 1


def test_force_is_a_fresh_report_not_an_update(refresh):
    refresh["records"] = [est()]
    go()
    snap = go(force=True)
    assert "refresh_count" not in snap
    assert len(refresh["pushed"]) == 2 and "(updated)" not in refresh["pushed"][1]


def test_refresh_off_keeps_the_old_once_a_day_behaviour(refresh, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 0.0)
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 999)
    assert go() is None and refresh["builds"] == 1


def test_refresh_does_not_run_on_a_weekend_or_when_disabled(refresh, monkeypatch):
    refresh["records"] = [est()]
    go()
    age_stored(refresh, 99)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", False)
    assert go() is None
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", True)
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: False)
    assert go() is None and refresh["builds"] == 1


# ── config ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [(None, 15.0), ("", 15.0), ("  ", 15.0), ("0", 0.0), ("5", 5.0), ("2.5", 2.5),
                                        ("-1", 15.0), ("abc", 15.0), ("nan", 15.0)])
def test_refresh_minutes_parsing(monkeypatch, raw, want):
    import config
    if raw is None:
        monkeypatch.delenv("DAILY_REPORT_REFRESH_MINUTES", raising=False)
    else:
        monkeypatch.setenv("DAILY_REPORT_REFRESH_MINUTES", raw)
    assert config._daily_report_refresh_minutes() == want


def test_refresh_defaults():
    import config
    assert config.DAILY_REPORT_REFRESH_UNTIL_IST == "18:00" or os.getenv("DAILY_REPORT_REFRESH_UNTIL_IST")


def test_save_is_false_when_an_overwrite_is_lost_and_the_old_snapshot_still_reads_back(cache):
    assert tr.save_daily_snapshot(object(), {"mode": "REAL", "day": "2026-10-09", "generated_at": "a"}) is True
    cache["lose_writes"] = True
    assert tr.save_daily_snapshot(object(), {"mode": "REAL", "day": "2026-10-09", "generated_at": "b"}) is False
    cache["lose_writes"] = False
    assert tr.save_daily_snapshot(object(), {"mode": "REAL", "day": "2026-10-09", "generated_at": "b"}) is True
