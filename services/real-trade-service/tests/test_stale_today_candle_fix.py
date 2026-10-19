"""2026-10-19: daily history (Dhan) has no candle for the live session -> 'today's return' was Friday's."""
from datetime import datetime
from zoneinfo import ZoneInfo

from candidate_engine import candidates as c

IST = ZoneInfo("Asia/Kolkata")
MON_11 = datetime(2026, 10, 19, 11, 12, tzinfo=IST)


def _hist(last_date="2026-10-16"):
    base = [{"date": f"2026-10-{d:02d} 00:00", "open": 200, "high": 206, "low": 198, "close": c_, "volume": 25000}
            for d, c_ in ((8, 201.0), (9, 202.0), (12, 202.11), (13, 203.64), (14, 203.28), (15, 203.29))]
    base.append({"date": f"{last_date} 00:00", "open": 204, "high": 206.9, "low": 202, "close": 204.32, "volume": 25208})
    return base


def _quote(**kw):
    q = {"price": 226.65, "previous_close": 204.32, "volume": 3640350, "day_high": 236.79, "day_low": 205.0, "source": "dhan"}
    q.update(kw)
    return q


def test_appends_synthetic_today_candle():
    out = c._with_live_today_candle(_hist(), _quote(), MON_11)
    assert len(out) == 8 and out[-1]["synthetic_from_quote"] and out[-1]["close"] == 226.65
    ret = (out[-1]["close"] / out[-2]["close"] - 1) * 100
    assert 10.8 < ret < 11.0            # was +0.5% (Friday's) before the fix


def test_no_change_when_history_already_has_today():
    h = _hist("2026-10-19")
    assert c._with_live_today_candle(h, _quote(), MON_11) is h


def test_no_change_on_weekend_or_preopen_or_stale_quote():
    h = _hist()
    sat = datetime(2026, 10, 17, 11, 0, tzinfo=IST)
    pre = datetime(2026, 10, 19, 9, 0, tzinfo=IST)
    assert c._with_live_today_candle(h, _quote(), sat) is h
    assert c._with_live_today_candle(h, _quote(), pre) is h
    assert c._with_live_today_candle(h, _quote(source="last_close_cache"), MON_11) is h
    assert c._with_live_today_candle(h, _quote(previous_close=190.0), MON_11) is h   # quote not from the next session
    assert c._with_live_today_candle(h, _quote(volume=0), MON_11) is h
