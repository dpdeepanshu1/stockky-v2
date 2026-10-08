"""group247 (Priority 4 of the 2026-10-08 log review): the standard candidate check's 6-month and 1-year views come from
one daily 1y series instead of two weekly yfinance calls, so a saturated yfinance bucket no longer leaves candidates
'cannot judge'. Falls back to the weekly calls when the daily series is missing or too short."""
import asyncio
from datetime import date, timedelta

import candidate_engine.candidates as cd


def run(coro):
    return asyncio.run(coro)


def _daily(n=250, start_px=100.0, step=0.2, end=date(2026, 10, 8)):
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        px = start_px + step * i
        out.append({"date": d.isoformat(), "open": px, "high": px + 1, "low": px - 1, "close": px + 0.1, "volume": 1000.0})
    return out


# ── the cutting helper ──────────────────────────────────────────────────────

def test_views_cut_six_months_and_keep_the_full_year():
    daily = _daily()
    half, year = cd._weekly_views_from_daily(daily)
    assert year is daily
    assert half[-1] is daily[-1]
    assert date.fromisoformat(half[0]["date"]) >= date(2026, 10, 8) - timedelta(days=183)
    assert date.fromisoformat(half[0]["date"]) < date(2026, 10, 8) - timedelta(days=180)
    assert 0 < len(half) < len(daily)


def test_views_use_what_exists_for_a_short_listing():
    half, year = cd._weekly_views_from_daily(_daily(n=100))          # ~100 days of history: both views are all of it
    assert len(half) == len(year) == 100


def test_views_are_refused_when_the_series_cannot_be_trusted():
    assert cd._weekly_views_from_daily(None) is None
    assert cd._weekly_views_from_daily(Exception("x")) is None
    assert cd._weekly_views_from_daily([]) is None
    assert cd._weekly_views_from_daily(_daily(n=30)) is None          # fewer than CANDIDATE_WEEKLY_FROM_DAILY_MIN_BARS
    nodate = [{"close": 1.0, "open": 1.0}] * 80
    assert cd._weekly_views_from_daily(nodate) is None
    junk = _daily(n=80)
    junk[-1]["date"] = "not-a-date"
    assert cd._weekly_views_from_daily(junk) is None
    same_day = [{"date": "2026-10-08", "close": 1.0, "open": 1.0}] * 80
    assert cd._weekly_views_from_daily(same_day) is None
    junk2 = _daily(n=80)
    junk2[0] = None
    assert cd._weekly_views_from_daily(junk2) is None


# ── _multi_tf_analysis: which calls it makes ────────────────────────────────

def _patch(monkeypatch, daily_answer, calls):
    async def fake_hist(client, symbol, period, interval="1d"):
        calls.append((period, interval))
        if (period, interval) == ("1y", "1d"):
            if isinstance(daily_answer, Exception):
                raise daily_answer
            return daily_answer
        if interval == "1wk":
            return _daily(n=26, step=1.0)
        return _daily(n=30, step=0.5)

    async def fake_quote(client, symbol):
        return {"price": 105.0, "previous_close": 104.0}

    monkeypatch.setattr(cd, "_fetch_history", fake_hist)
    monkeypatch.setattr(cd, "_fetch_quote", fake_quote)


def test_no_weekly_call_is_made_when_the_daily_series_is_usable(monkeypatch):
    calls = []
    _patch(monkeypatch, _daily(), calls)
    res = run(cd._multi_tf_analysis(object(), "ABC"))
    assert isinstance(res, dict)
    assert not any(itv == "1wk" for _p, itv in calls)
    assert ("1y", "1d") in calls and ("2y", "1mo") in calls and ("1d", "60m") in calls


def test_the_weekly_calls_run_when_the_daily_series_is_missing(monkeypatch):
    for answer in ([], Exception("market-data down"), _daily(n=20)):
        calls = []
        _patch(monkeypatch, answer, calls)
        run(cd._multi_tf_analysis(object(), "ABC"))
        assert ("6mo", "1wk") in calls and ("1y", "1wk") in calls


def test_the_weekly_calls_are_made_when_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(cd, "WEEKLY_FROM_DAILY", False)
    calls = []
    _patch(monkeypatch, _daily(), calls)
    run(cd._multi_tf_analysis(object(), "ABC"))
    assert ("6mo", "1wk") in calls and ("1y", "1wk") in calls
    assert ("1y", "1d") not in calls


def test_derived_views_give_the_same_returns_and_52w_range_as_weekly_bars(monkeypatch):
    # a steadily rising stock near its 52-week high: the 52-week check must still see the daily high/low
    calls = []
    daily = _daily(n=250, step=0.4)                         # ends at the high
    _patch(monkeypatch, daily, calls)
    res = run(cd._multi_tf_analysis(object(), "ABC"))
    # price 105 is far below the daily series' 52-week high (~200) -> not overextended, so the verdict is not range_52w
    assert res.get("reject_kind") != "range_52w"
    half, year = cd._weekly_views_from_daily(daily)
    assert cd._pct_return(year) == round((daily[-1]["close"] / daily[0]["open"] - 1) * 100, 2)
    assert cd._pct_return(half) == round((half[-1]["close"] / half[0]["open"] - 1) * 100, 2)


def test_views_are_refused_when_the_six_month_cut_would_hold_a_single_bar():
    old = _daily(n=79, end=date(2026, 1, 1))
    last = _daily(n=1, end=date(2026, 10, 8))
    assert cd._weekly_views_from_daily(old + last) is None
