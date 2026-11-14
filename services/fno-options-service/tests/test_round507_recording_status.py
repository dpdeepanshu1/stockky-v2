"""Round 507 (E527): the runner says when today's recording really began, counts chains it wanted and did not get, and starts a new day's counters at midnight (snapshots_today counted since the process started)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from fno.provider import IST
from fno.runtime import Runner
from test_engine_paper import FakeProvider, engine, make_cfg
from test_engine_paper import db  # noqa: F401  (fixture)

OPEN = datetime(2026, 11, 10, 9, 16, tzinfo=IST)         # a Tuesday


class NoChain(FakeProvider):
    def chain(self, u, e):
        return None


def _runner(db, tmp_path, prov=None):
    e = engine(db, prov or FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"))
    r = Runner(e, e.cfg, str(tmp_path))
    r.started_at = OPEN.timestamp() - 3600
    return r


def test_recording_from_the_open_is_not_late(db, tmp_path):
    r = _runner(db, tmp_path)
    r.snapshot(OPEN)
    s = r.recording_status(OPEN)
    assert s["first_snapshot"] == "09:16" and s["late"] is False and s["snapshots_today"] == 1 and s["detail"] == ""


def test_a_first_snapshot_after_0920_is_late_and_says_when_the_service_started(db, tmp_path):
    r = _runner(db, tmp_path)
    r.started_at = datetime(2026, 11, 10, 11, 24, tzinfo=IST).timestamp()
    r.snapshot(datetime(2026, 11, 10, 11, 25, tzinfo=IST))
    s = r.recording_status(datetime(2026, 11, 10, 11, 26, tzinfo=IST))
    assert s["late"] is True and "first snapshot at 11:25" in s["detail"] and "started at 11:24" in s["detail"]


def test_no_snapshot_yet_in_market_hours_is_late_but_before_the_open_or_at_the_weekend_it_is_not(db, tmp_path):
    r = _runner(db, tmp_path)
    assert r.recording_status(datetime(2026, 11, 10, 10, 0, tzinfo=IST))["late"] is True
    assert r.recording_status(datetime(2026, 11, 10, 9, 10, tzinfo=IST))["late"] is False
    assert r.recording_status(datetime(2026, 11, 14, 10, 0, tzinfo=IST))["late"] is False        # Saturday
    assert r.recording_status(datetime(2026, 11, 10, 10, 0, tzinfo=IST), holidays=("2026-11-10",))["late"] is False


def test_a_chain_that_does_not_come_is_counted_and_logged_once_per_five_minutes(db, tmp_path, caplog):
    r = _runner(db, tmp_path, NoChain())
    with caplog.at_level(logging.WARNING, logger="fno.runtime"):
        for i in range(4):
            r.snapshot(OPEN + timedelta(seconds=61 * i))
    assert r.chain_misses == {"NIFTY": 4} and r.snapshots_today == 0
    assert len([x for x in caplog.records if "no snapshot of NIFTY" in x.getMessage()]) == 1


def test_a_new_day_starts_the_counters_again(db, tmp_path):
    r = _runner(db, tmp_path)
    r.snapshot(OPEN)
    assert r.snapshots_today == 1
    nxt = OPEN + timedelta(days=1)
    r.snapshot(nxt)
    assert r.snapshots_today == 1 and r.recording_status(nxt)["first_snapshot"] == "09:16"
