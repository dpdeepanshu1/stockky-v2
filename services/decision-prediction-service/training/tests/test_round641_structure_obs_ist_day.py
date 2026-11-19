"""Round 641 (E667): structure_obs.evaluate_pending and structure_obs.report used date.today(), which is the UTC date on the servers, so from 00:00 to 05:30 IST it was the previous IST day.
It now uses the IST date (`_ist_today`). Plain functions; `python3 <this file>` runs them without pytest. No network."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import structure_obs as M  # noqa: E402


def test_ist_today_is_the_ist_calendar_day():
    assert M._ist_today(datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)    # 01:30 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 18, 18, 29, tzinfo=timezone.utc)) == date(2026, 11, 18)   # 23:59 IST on the 18th
    assert M._ist_today(datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc)) == date(2026, 11, 19)   # 00:00 IST on the 19th
    assert M._ist_today(datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)) == date(2026, 11, 19)


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def limit(self, *a):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    def query(self, M_):
        return _Q(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _Col:
    def in_(self, *a):
        return self

    def desc(self):
        return self


class _M:
    id = _Col()
    eval_status = _Col()


class _Row:
    def __init__(self, **kw):
        self.eval_status, self.eval_attempts = "pending", 0
        for k, v in kw.items():
            setattr(self, k, v)


def _freeze_clock(utc_now):
    """Make structure_obs read `utc_now` as the current time (it only calls datetime.now(timezone.utc))."""
    real = M.datetime

    class _FakeDT(real):
        @classmethod
        def now(cls, tz=None):
            return utc_now.astimezone(tz) if tz else utc_now.replace(tzinfo=None)

    M.datetime = _FakeDT
    return real


# 20:00 UTC on 18 Nov is 01:30 IST on 19 Nov: the server's own date is the 18th, the IST day is the 19th.
_NIGHT = datetime(2026, 11, 18, 20, 0, tzinfo=timezone.utc)


def test_report_without_a_date_counts_the_window_from_the_ist_day():
    rows = [_Row(symbol="EDGE", obs_date="2026-11-09"), _Row(symbol="OLD", obs_date="2026-11-08"), _Row(symbol="NEW", obs_date="2026-11-18")]
    real = _freeze_clock(_NIGHT)
    try:
        out = M.report(_DB(rows), _M, days=10, min_n=1)
    finally:
        M.datetime = real
    # IST day 19 Nov - 10 days = 9 Nov. From the UTC day it would be 8 Nov and OLD would be counted.
    assert out["since"] == "2026-11-09", out["since"]
    assert out["window_days"] == 10 and out["total"] == 2, out["total"]


def test_report_explicit_today_still_wins():
    rows = [_Row(symbol="A", obs_date="2026-10-10")]
    real = _freeze_clock(_NIGHT)
    try:
        out = M.report(_DB(rows), _M, days=30, min_n=1, today=date(2026, 10, 15))
    finally:
        M.datetime = real
    assert out["since"] == "2026-09-15" and out["total"] == 1


def _pending(obs_date):
    return _Row(symbol="NODATA", obs_date=obs_date)


def test_evaluate_pending_give_up_age_is_counted_from_the_ist_day():
    # 4 Oct to the IST day (19 Nov) is 46 days: past GIVE_UP_DAYS (45). To the UTC day (18 Nov) it is 45: not yet.
    assert M.GIVE_UP_DAYS == 45
    old, edge = _pending("2026-10-04"), _pending("2026-10-05")
    real = _freeze_clock(_NIGHT)
    try:
        res = M.evaluate_pending(_DB([old, edge]), _M, lambda sym: [], None, max_batch=10)
    finally:
        M.datetime = real
    assert old.eval_status == "no_data", old.eval_status      # 46 days by the IST day
    assert edge.eval_status == "pending", edge.eval_status    # 45 days: not given up yet
    assert res["no_data"] == 1 and res["waiting"] == 1, res


def test_evaluate_pending_after_ist_midnight_matches_a_daytime_run():
    # The same rows give the same answer at 01:30 IST and at 12:00 IST on the 19th.
    noon = datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc)
    outs = []
    for now in (_NIGHT, noon):
        old, edge = _pending("2026-10-04"), _pending("2026-10-05")
        real = _freeze_clock(now)
        try:
            M.evaluate_pending(_DB([old, edge]), _M, lambda sym: [], None, max_batch=10)
        finally:
            M.datetime = real
        outs.append((old.eval_status, edge.eval_status))
    assert outs[0] == outs[1] == ("no_data", "pending"), outs


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
