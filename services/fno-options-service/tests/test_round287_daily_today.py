"""Round 287: the live direction logic reads the daily index candles up to YESTERDAY, like every backtest. market-data serves the session still running as the last daily candle (it repaints every minute);
the backtests (sim / rolling / replay) never see it. No network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import config
from fno import signals
from fno.provider import HttpProvider

IST = timezone(timedelta(hours=5, minutes=30))


class _Resp:
    def __init__(self, body):
        self.b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.b


def _candles(n_before: int, today_close: float):
    """n_before rising daily candles up to yesterday + a row for TODAY (the forming session). market-data labels are 'YYYY-MM-DD'."""
    today = datetime.now(IST).date()
    rows, px = [], 20000.0
    for k in range(n_before, 0, -1):
        d = today - timedelta(days=k)
        o, c = px, px + 40.0
        rows.append({"date": d.isoformat(), "open": o, "high": c + 20, "low": o - 20, "close": c, "volume": 0})
        px = c
    rows.append({"date": today.isoformat(), "open": px, "high": px + 10, "low": today_close - 20, "close": today_close, "volume": 0})
    return rows, today


def _provider(rows, **kw):
    return HttpProvider("http://md", 1.0, opener=lambda req, timeout=None: _Resp({"candles": rows}), **kw)


def test_todays_forming_daily_candle_is_left_out_by_default():
    rows, today = _candles(60, 19000.0)
    got = _provider(rows).daily("NIFTY")
    assert len(got) == 60 and all(datetime.fromtimestamp(c["t"], IST).date() < today for c in got)


def test_the_old_behaviour_is_one_env_switch_away():
    rows, today = _candles(60, 19000.0)
    got = _provider(rows, daily_exclude_today=False).daily("NIFTY")
    assert len(got) == 61 and datetime.fromtimestamp(got[-1]["t"], IST).date() == today


def test_the_cut_is_made_on_every_call_not_only_when_the_data_is_fetched(monkeypatch):
    rows, today = _candles(60, 19000.0)
    p = _provider(rows)
    assert len(p.daily("NIFTY")) == 60                      # fetched and cached
    import fno.provider as prov

    class _Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)

    monkeypatch.setattr(prov, "datetime", _Tomorrow)
    assert len(p.daily("NIFTY")) == 61                      # the next day the same cached list holds one more finished day


def test_it_matters_a_forming_crash_candle_flips_the_daily_score():
    rows, _ = _candles(60, 19000.0)                         # 60 days up, then today trades 5 % lower so far
    with_today = _provider(rows, daily_exclude_today=False).daily("NIFTY")
    without = _provider(rows).daily("NIFTY")
    a, b = signals.daily_score(with_today)[0], signals.daily_score(without)[0]
    assert b - a >= 15 and b > 0                            # what the backtests would have seen: an uptrend (+25); live used to see the repainting candle (+5)


def test_the_default_is_on():
    assert config.DAILY_EXCLUDE_TODAY is True
