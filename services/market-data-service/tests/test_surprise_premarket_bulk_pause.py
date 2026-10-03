"""
tests/test_surprise_premarket_bulk_pause.py — pacing of bulk_baselines_from_yfinance

The pause between yf.download() batches used to sit at the bottom of the loop, after the `continue`s
for a failed or empty download, so exactly the batches that most likely hit a rate limit got no pause
before the next call. It now sits before every batch except the first.

No network, no real yfinance/pandas: both are tiny fakes in sys.modules (numpy is the real one) and the
module's `time` is a fake clock, so nothing sleeps.

Run from services/market-data-service:
    python3 -m pytest tests/test_surprise_premarket_bulk_pause.py -v
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

import surprise_premarket as sp


class _Clock:
    def __init__(self):
        self.sleeps = []
        self.order = []

    def sleep(self, s):
        self.order.append("sleep")
        self.sleeps.append(s)

    def time(self):
        return 0.0


class _Series:
    def __init__(self, vals):
        self.values = np.array(vals, dtype="float64")

    def astype(self, _dt):
        return self


class _Frame:
    """Single-ticker OHLCV frame over plain lists."""

    def __init__(self, n=10):
        self.n = n
        self.columns = ["Close", "High", "Low", "Volume"]
        self._cols = {"Close": [100.0] * n, "High": [110.0] * n, "Low": [90.0] * n, "Volume": [25000.0] * n}

    @property
    def empty(self):
        return self.n == 0

    def dropna(self, how=None):
        return self

    def __len__(self):
        return self.n

    def tail(self, k):
        f = _Frame(min(self.n, k))
        return f

    def __getitem__(self, key):
        return _Series(self._cols[key][: self.n])


class _MI:
    pass


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(sp, "time", clock)
    monkeypatch.setattr(sp, "_yahoo_session", lambda: None)

    state = types.SimpleNamespace(results=[], downloads=[], clock=clock)

    yf = types.ModuleType("yfinance")

    def download(tickers, **kw):
        clock.order.append("download")
        state.downloads.append(tickers)
        r = state.results.pop(0) if state.results else None
        if isinstance(r, Exception):
            raise r
        return r

    yf.download = download
    pd = types.ModuleType("pandas")
    pd.MultiIndex = _MI
    rl = types.ModuleType("rate_limiter")
    rl.acquire = lambda name, weight=1: None
    for name, mod in (("yfinance", yf), ("pandas", pd), ("rate_limiter", rl)):
        monkeypatch.setitem(sys.modules, name, mod)
    return state


class TestBulkBatchPacing:
    def test_pause_between_successful_batches_only(self, env):
        env.results = [_Frame(), _Frame(), _Frame()]
        sp.bulk_baselines_from_yfinance([f"S{i}" for i in range(5)], batch_size=2)
        assert [len(t.split()) for t in env.downloads] == [2, 2, 1]
        assert env.clock.sleeps == [sp.YF_BULK_BATCH_PAUSE, sp.YF_BULK_BATCH_PAUSE]

    def test_pause_still_happens_after_failed_and_empty_batches(self, env):
        env.results = [RuntimeError("429"), None, _Frame()]
        sp.bulk_baselines_from_yfinance([f"S{i}" for i in range(5)], batch_size=2)
        assert len(env.downloads) == 3
        assert env.clock.sleeps == [sp.YF_BULK_BATCH_PAUSE, sp.YF_BULK_BATCH_PAUSE]

    def test_empty_frame_batch_is_followed_by_a_pause(self, env):
        env.results = [_Frame(n=0), _Frame()]
        sp.bulk_baselines_from_yfinance(["A", "B"], batch_size=1)
        assert env.clock.sleeps == [sp.YF_BULK_BATCH_PAUSE]

    def test_pause_lands_between_downloads_not_after_the_last(self, env):
        env.results = [RuntimeError("429"), RuntimeError("429")]
        sp.bulk_baselines_from_yfinance(["A", "B"], batch_size=1)
        assert env.clock.order == ["download", "sleep", "download"]

    def test_single_batch_never_pauses(self, env):
        env.results = [RuntimeError("429")]
        sp.bulk_baselines_from_yfinance(["A", "B"], batch_size=50)
        assert env.clock.sleeps == []

    def test_failed_batch_symbols_stay_remaining(self, env):
        env.results = [RuntimeError("429"), _Frame()]
        rows, remaining = sp.bulk_baselines_from_yfinance(["AAA", "BBB"], batch_size=1)
        assert [r["symbol"] for r in rows] == ["BBB"] and remaining == ["AAA"]
