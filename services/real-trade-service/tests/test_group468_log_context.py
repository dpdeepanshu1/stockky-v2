"""group468: log-only Mansfield RS + breadth (candidate_engine/log_context.py). Pure / stdlib only; nothing here gates a trade."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("g468_log_context", os.path.join(_ROOT, "candidate_engine", "log_context.py"))
lc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lc)


def _candles(closes, start=date(2026, 1, 1)):
    return [{"date": (start + timedelta(days=i)).isoformat(), "open": c, "close": c} for i, c in enumerate(closes)]


def test_mansfield_hand_computed():
    stock = lc._series(_candles([10, 10, 10, 12]))
    bench = lc._series(_candles([10, 10, 10, 10]))
    # RS = 1, 1, 1, 1.2 ; SMA(4) = 1.05 ; (1.2/1.05 - 1) * 10 = 1.428571...
    r = lc.mansfield_rs(stock, bench, 4)
    assert r == {"value": 1.429, "bars": 4}


def test_mansfield_negative_when_lagging_and_none_without_enough_common_dates():
    stock = lc._series(_candles([10, 10, 10, 9]))
    bench = lc._series(_candles([10, 10, 10, 10]))
    assert lc.mansfield_rs(stock, bench, 4)["value"] < 0
    assert lc.mansfield_rs(stock, bench, 5) is None
    assert lc.mansfield_rs({}, bench, 4) is None
    shifted = lc._series(_candles([10] * 4, start=date(2027, 1, 1)))      # no common dates
    assert lc.mansfield_rs(stock, shifted, 4) is None


def test_series_skips_bad_rows():
    s = lc._series([{"date": "2026-01-01", "close": 5}, {"date": "", "close": 5}, {"date": "2026-01-02", "close": None},
                    {"date": "2026-01-03", "close": -1}, None, {"date": "2026-01-04", "close": "x"}])
    assert s == {"2026-01-01": 5.0}
    assert lc._series("nope") == {}


def test_ema_and_above_ema():
    assert lc.ema_last([1, 2, 3], 5) is None
    assert abs(lc.ema_last([10, 10, 10, 10], 3) - 10) < 1e-9
    rising = lc._series(_candles([float(i + 1) for i in range(60)]))
    falling = lc._series(_candles([float(100 - i) for i in range(60)]))
    assert lc.above_ema(rising, 50) is True and lc.above_ema(falling, 50) is False
    assert lc.above_ema(lc._series(_candles([1, 2, 3])), 50) is None


def test_breadth_needs_enough_fresh_readings_and_expires():
    lc.clear_state()
    for i in range(10):
        lc.record_breadth(f"S{i}", i < 5, now=1000.0)
    assert lc.breadth_reading(now=1001.0) == {"breadth_pct": None, "breadth_n": 10}      # under the minimum sample
    for i in range(10, 20):
        lc.record_breadth(f"S{i}", i < 15, now=1000.0)
    assert lc.breadth_reading(now=1001.0) == {"breadth_pct": 50.0, "breadth_n": 20}
    assert lc.breadth_reading(now=1000.0 + 4000) == {"breadth_pct": None, "breadth_n": 0}  # all stale
    lc.record_breadth("X", None)                                                          # None is never recorded
    assert "X" not in lc._breadth
    lc.clear_state()


def test_context_for_end_to_end_caches_benchmark_and_never_raises(monkeypatch=None):
    lc.clear_state()
    calls = []

    async def fetch(client, symbol, period, interval):
        calls.append(symbol)
        return _candles([100.0] * 260)

    stock = _candles([100.0 + i * 0.5 for i in range(260)])

    async def run():
        a = await lc.context_for(fetch, None, "AAA", stock)
        b = await lc.context_for(fetch, None, "BBB", stock)
        return a, b

    a, b = asyncio.run(run())
    assert a["mansfield_rs"] is not None and a["mansfield_rs"] > 0 and a["rs_bars"] == 200 and a["above_ema50"] is True
    assert a["bench"] == "^NSEI" and a["breadth_pct"] is None            # fewer than 20 symbols read so far
    assert calls == ["^NSEI"]                                             # the benchmark was fetched once, then cached

    async def boom(*a, **k):
        raise RuntimeError("down")

    lc.clear_state()
    assert asyncio.run(lc.context_for(boom, None, "CCC", stock)) is None       # a failing fetch -> None, no exception
    short = asyncio.run(lc.context_for(fetch, None, "DDD", _candles([1.0] * 10)))
    assert short["mansfield_rs"] is None and short["above_ema50"] is None      # too short: recorded as unknown, not guessed
    lc.clear_state()


def test_switch_off_returns_none():
    os.environ["CANDIDATE_LOG_CONTEXT"] = "0"
    try:
        async def fetch(*a, **k):
            return []
        assert asyncio.run(lc.context_for(fetch, None, "AAA", _candles([1.0] * 300))) is None
    finally:
        os.environ.pop("CANDIDATE_LOG_CONTEXT", None)
