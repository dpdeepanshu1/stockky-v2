"""GENERATED COPY of shared/market_structure/tests/test_timeframes.py (imports the vendored stockky_ms) so the service coverage gate sees it.
Edit the shared file, then re-copy (sed s/market_structure/stockky_ms/)."""
import datetime as _dt, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockky_ms import timeframes as TF
from stockky_ms.ms_types import IST_OFFSET_S


def ist_epoch(day: str, hhmm: str) -> float:
    d = _dt.datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=_dt.timezone.utc)
    return d.timestamp() - IST_OFFSET_S


def minute_rows(day: str, first="09:15", n=375, px=100.0, step=0.01):
    t0 = ist_epoch(day, first)
    rows = []
    for k in range(n):
        ts = t0 + 60 * k
        label = _dt.datetime.fromtimestamp(ts + IST_OFFSET_S, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        o = px + step * k
        rows.append({"date": label, "open": o, "high": o + 0.05, "low": o - 0.05, "close": o + step, "volume": 10 + k})
    return rows


def test_spec_aliases_and_fetch_plan():
    assert TF.TIMEFRAMES == ("1m", "5m", "15m", "30m", "1h", "1d")
    assert TF.spec("60m")["key"] == "1h" and TF.spec("DAILY")["key"] == "1d" and TF.spec("7m") is None and TF.spec(None) is None
    assert TF.spec("30m")["fetch"] == "15m" and TF.spec("30m")["minutes"] == 30
    assert TF.spec("1m")["period"] == "5d" and TF.spec("1d")["intraday"] is False and TF.spec("5m")["unit"] == "candle"
    assert TF.spec("1m")["note"] and TF.spec("5m")["note"] and TF.spec("15m")["note"] is None
    assert TF.norm_tf("zz") == "1d" and TF.norm_tf("zz", "5m") == "5m"


def test_clamp_tail():
    assert TF.clamp_tail("1m") == 750 and TF.clamp_tail("1d") == 300
    assert TF.clamp_tail("1d", 9999) == 500 and TF.clamp_tail("5m", 9999) == TF.MAX_TAIL
    assert TF.clamp_tail("5m", 3) == 30 and TF.clamp_tail("5m", "x") == 600


def test_naive_dates_are_ist_and_offsets_are_honoured():
    want = ist_epoch("2026-10-22", "09:15")
    assert TF.parse_row_ts("2026-10-22 09:15") == want
    assert TF.parse_row_ts("2026-10-22T09:15:00+05:30") == want
    assert TF.parse_row_ts("2026-10-22T03:45:00Z") == want
    assert TF.parse_row_ts(want) == want and TF.parse_row_ts(want * 1000) == want
    assert TF.label_for(want, "5m") == "2026-10-22 09:15" and TF.label_for(want, "1d") == "2026-10-22"
    assert TF.parse_row_ts("2026-10-22") == ist_epoch("2026-10-22", "00:00")
    assert TF.parse_row_ts("nope") is None and TF.parse_row_ts(None) is None and TF.parse_row_ts(True) is None


def test_rows_to_bars_drops_junk_and_keeps_the_later_duplicate():
    rows = minute_rows("2026-10-22", n=3)
    rows.append(dict(rows[1], close=105.0, high=106.0))
    rows += [None, "x", {"date": "bad", "open": 1, "high": 1, "low": 1, "close": 1}, {"date": rows[0]["date"], "high": float("nan"), "low": 1, "close": 1},
             {"date": "2026-10-22 09:20", "open": 0, "high": 0, "low": 0, "close": 0}, {"date": "2026-10-22 09:21", "open": 1, "high": 1, "low": 2, "close": 1}]
    bars = TF.rows_to_bars(rows)
    assert len(bars) == 3 and bars[1][4] == 105.0 and bars == sorted(bars)
    assert TF.rows_to_bars(None) == [] and TF.rows_to_bars("x") == []


def test_five_minute_bars_from_one_minute_rows():
    r = TF.session_bars(minute_rows("2026-10-22"), "5m")
    assert len(r["closed"]) == 75 and r["live"] is None and r["labels"][0] == "2026-10-22 09:15" and r["labels"][-1] == "2026-10-22 15:25"
    first = r["closed"][0]
    assert first[1] == 100.0 and abs(first[4] - 100.05) < 1e-9 and first[5] == sum(10 + k for k in range(5))
    assert first[2] == max(b["high"] for b in minute_rows("2026-10-22")[:5]) and first[3] == min(b["low"] for b in minute_rows("2026-10-22")[:5])
    assert len(set(r["labels"])) == len(r["labels"])


def test_one_minute_timeframe_keeps_every_minute():
    r = TF.session_bars(minute_rows("2026-10-22"), "1m")
    assert len(r["closed"]) == 375 and r["labels"][-1] == "2026-10-22 15:29"


def test_live_bar_is_the_one_still_forming():
    rows = minute_rows("2026-10-22", n=53)
    now = ist_epoch("2026-10-22", "10:07") + 30
    r5 = TF.session_bars(rows, "5m", now)
    assert r5["live_label"] == "2026-10-22 10:05" and r5["labels"][-1] == "2026-10-22 10:00" and r5["live"][5] == 10 + 50 + 10 + 51 + 10 + 52
    r30 = TF.session_bars(rows, "30m", now)
    assert r30["live_label"] == "2026-10-22 09:45" and r30["labels"][-1] == "2026-10-22 09:15"
    assert TF.session_bars(rows, "5m", None)["live"] is None and len(TF.session_bars(rows, "5m", None)["closed"]) == 11


def test_last_bucket_of_the_day_closes_at_1530_not_at_the_bucket_width():
    rows = minute_rows("2026-10-22")
    after = ist_epoch("2026-10-22", "15:31")
    for tf in ("30m", "1h"):
        r = TF.session_bars(rows, tf, after)
        assert r["live"] is None and r["labels"][-1] == "2026-10-22 15:15", tf
    r = TF.session_bars(rows, "30m", ist_epoch("2026-10-22", "15:20"))
    assert r["live_label"] == "2026-10-22 15:15"
    assert TF.session_bars(rows, "1h", after)["labels"] == [f"2026-10-22 {h}:15" for h in ("09", "10", "11", "12", "13", "14", "15")]


def test_two_sessions_never_merge_and_out_of_session_minutes_are_dropped():
    rows = minute_rows("2026-10-21") + minute_rows("2026-10-22")
    rows += [{"date": "2026-10-22 09:10", "open": 1, "high": 1, "low": 1, "close": 1}, {"date": "2026-10-22 15:30", "open": 1, "high": 1, "low": 1, "close": 1}]
    r = TF.session_bars(rows, "30m")
    assert len(r["closed"]) == 26 and r["labels"][12] == "2026-10-21 15:15" and r["labels"][13] == "2026-10-22 09:15"
    assert not any(l.endswith("09:10") or l.endswith("15:30") for l in TF.session_bars(rows, "1m")["labels"])


def test_30m_from_15m_rows_and_1h_from_native_hourly_rows():
    r15 = [{"date": f"2026-10-22 {h:02d}:{m:02d}", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 5}
           for h, m in [(9, 15), (9, 30), (9, 45), (10, 0), (10, 15)]]
    r = TF.session_bars(r15, "30m", ist_epoch("2026-10-22", "10:30"))
    assert r["labels"] == ["2026-10-22 09:15", "2026-10-22 09:45"] and r["live_label"] == "2026-10-22 10:15" and r["closed"][0][5] == 10
    hourly = [{"date": f"2026-10-22 {h:02d}:15", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 7} for h in (9, 10, 11)]
    assert TF.session_bars(hourly, "1h", ist_epoch("2026-10-22", "12:30"))["labels"][-1] == "2026-10-22 11:15"


def test_lag_says_how_far_behind_the_data_is():
    rows = minute_rows("2026-10-22", n=46)
    r = TF.session_bars(rows, "1m", ist_epoch("2026-10-22", "10:07"))
    assert r["lag_s"] == 6 * 60 and r["last_base_ts"] == ist_epoch("2026-10-22", "10:00")
    assert TF.session_bars(rows, "1m", None)["lag_s"] is None
    assert TF.session_bars(minute_rows("2026-10-22", n=53), "1m", ist_epoch("2026-10-22", "10:07") + 10)["lag_s"] == 0
    five = [r_ for r_ in minute_rows("2026-10-22", n=46) if int(r_["date"][-2:]) % 5 == 0]
    assert TF.session_bars(five, "5m", ist_epoch("2026-10-22", "10:07"))["lag_s"] == 120


def test_daily_rows_one_bar_per_ist_date_and_today_is_live_until_the_close():
    rows = [{"date": f"2026-10-{d} 00:00", "open": 100 + d, "high": 102 + d, "low": 99 + d, "close": 101 + d, "volume": 1000} for d in (19, 20, 21, 22)]
    closed = TF.session_bars(rows, "1d", ist_epoch("2026-10-23", "08:00"))
    assert closed["labels"] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"] and closed["live"] is None
    open_ = TF.session_bars(rows, "1d", ist_epoch("2026-10-22", "11:00"))
    assert open_["labels"][-1] == "2026-10-21" and open_["live_label"] == "2026-10-22"
    after = TF.session_bars(rows, "1d", ist_epoch("2026-10-22", "15:45"))
    assert after["live"] is None and after["labels"][-1] == "2026-10-22"
    assert TF.session_bars(rows, "1d", None)["live"] is None


def test_unknown_timeframe_and_no_rows_never_raise():
    for tf in ("7m", None, "", 5):
        assert TF.session_bars(minute_rows("2026-10-22", n=5), tf)["closed"] == []
    assert TF.session_bars([], "5m")["closed"] == [] and TF.session_bars(None, "5m")["labels"] == []


def test_span_and_session_break():
    assert TF.span_of("morning_star") == 3 and TF.span_of("bullish_engulfing") == 2 and TF.span_of("hammer") == 1 and TF.span_of("nope") == 1
    r = TF.session_bars(minute_rows("2026-10-21") + minute_rows("2026-10-22"), "15m")["closed"]
    first_of_day2 = 25
    assert TF.crosses_session(r, first_of_day2, 2) is True and TF.crosses_session(r, first_of_day2, 1) is False
    assert TF.crosses_session(r, first_of_day2 + 1, 2) is False and TF.crosses_session(r, first_of_day2 + 1, 3) is True
    assert TF.crosses_session(r, 0, 3) is False and TF.crosses_session(None, 5, 2) is False
