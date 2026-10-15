"""group 402: market-relative returns on the real-trade structure observations - the evaluator fills bench_ret_{1,5,10,20}d, a benchmark that
cannot be fetched leaves them empty (never 0, never blanking a stored value), and the report shows the edge with the market taken out.
Runs with pytest or plain python; SQLAlchemy touch points are replaced by in-memory fakes exactly as in test_group356_structure_observation.py."""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import structure_observation as so

DAY = 86400.0
NOW = 1_700_000_000.0 + 6 * 3600          # a trading-hours stamp
BENCH_COLS = ("bench_ret_1d", "bench_ret_5d", "bench_ret_10d", "bench_ret_20d")


class FakeRow:
    def __init__(self, **kw):
        self.evaluated_at = None
        self.id = None
        for k in ("ret_1d", "ret_5d", "ret_10d", "ret_20d", "mfe_pct", "mae_pct", "first_touch") + BENCH_COLS:
            setattr(self, k, None)
        self.bars_seen = 0
        self.is_complete = False
        self.__dict__.update(kw)


class FakeDb:
    def __init__(self):
        self.rows, self.commits = [], 0

    def add(self, r):
        self.rows.append(r)

    def commit(self):
        self.commits += 1


def _patched(db):
    st = ExitStack()
    st.enter_context(mock.patch.object(so, "_model", lambda: FakeRow))
    st.enter_context(mock.patch.object(so, "_exists", lambda d, m, s, day: any(r.mode == m and r.symbol == s and r.obs_day == day for r in d.rows)))
    st.enter_context(mock.patch.object(so, "_pending", lambda d, n: sorted([r for r in d.rows if not r.is_complete], key=lambda r: r.obs_ts)[:n]))
    st.enter_context(mock.patch.object(config, "STRUCTURE_OBS_ENABLED", True, create=True))
    so.reset()
    return st


SUMMARY = {"ms_quality": "ok", "ms_stop": 95.0, "ms_target": 110.0, "ms_atr": 2.0, "ms_tqs": 61, "ms_trend": "bullish"}
VERDICT = {"opinion": "ok", "would_reject": False, "reasons": []}


def _bars(obs_ts, closes, with_obs_day=False):
    """Daily bars after the observation day (and, for the benchmark, one ON the observation day at closes[0])."""
    from stockky_ms.candles import day_key
    k = day_key(obs_ts)
    first = k if with_obs_day else k + 1
    return [(float((first + i) * 86400), c, c + 1.0, c - 1.0, c, 1000.0) for i, c in enumerate(closes)]


def _seed(db, symbol="TCS", ts=NOW):
    so.record(db, symbol, "REAL", 100.0, VERDICT, SUMMARY, "enforce", False, ts)


STOCK = [101 + i for i in range(25)]                          # +1% .. +25% after the entry price of 100
BENCH = [100.0] + [100.0 + 0.5 * i for i in range(1, 26)]     # the market: 100 on the observation day, then +0.5 per session


def test_evaluate_fills_the_benchmark_returns_next_to_the_stock_returns():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        res = so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK), fetch_bench=lambda lb: _bars(NOW, BENCH, True))
    r = db.rows[0]
    assert res["updated"] == 1 and (r.ret_1d, r.ret_20d) == (1.0, 20.0)
    assert (r.bench_ret_1d, r.bench_ret_5d, r.bench_ret_10d, r.bench_ret_20d) == (0.5, 2.5, 5.0, 10.0)


def test_benchmark_is_fetched_once_per_pass_with_120_days():
    db = FakeDb()
    calls = []
    def fb(lb):
        calls.append(lb)
        return _bars(NOW, BENCH, True)
    with _patched(db):
        _seed(db, "TCS")
        _seed(db, "INFY")
        so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK), fetch_bench=fb)
    assert calls == [120] and all(r.bench_ret_5d == 2.5 for r in db.rows) and len(db.rows) == 2


def test_failed_benchmark_leaves_the_fields_empty_and_keeps_the_stock_outcome():
    db = FakeDb()
    def down(lb):
        raise RuntimeError("history down")
    with _patched(db):
        _seed(db)
        res = so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK), fetch_bench=down)
    r = db.rows[0]
    assert res["updated"] == 1 and r.ret_20d == 20.0 and r.is_complete is True
    assert all(getattr(r, c) is None for c in BENCH_COLS)                  # unknown, never 0


def test_a_later_pass_with_no_benchmark_never_blanks_stored_values():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        so.evaluate_pending(db, now_ts=NOW + 4 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK[:3]), fetch_bench=lambda lb: _bars(NOW, BENCH[:4], True))
        assert db.rows[0].bench_ret_1d == 0.5 and db.rows[0].bench_ret_5d is None
        db.rows[0].evaluated_at = None                                      # force the next pass to look at the row again
        so.evaluate_pending(db, now_ts=NOW + 5 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK[:4]), fetch_bench=lambda lb: [])
    assert db.rows[0].bench_ret_1d == 0.5


def test_default_path_fetches_the_configured_benchmark_symbol_and_an_empty_symbol_switches_it_off():
    seen = []
    def fake_fetch(sym, lb):
        seen.append(sym)
        return _bars(NOW, BENCH, True) if sym == "NIFTY" else _bars(NOW, STOCK)
    db = FakeDb()
    with _patched(db), mock.patch.object(so, "fetch_daily_bars", fake_fetch), mock.patch.object(config, "STRUCTURE_OBS_BENCH_SYMBOL", "NIFTY", create=True):
        _seed(db)
        so.evaluate_pending(db, now_ts=NOW + 30 * DAY)
    assert sorted(seen) == ["NIFTY", "TCS"] and db.rows[0].bench_ret_20d == 10.0
    seen.clear()
    db2 = FakeDb()
    with _patched(db2), mock.patch.object(so, "fetch_daily_bars", fake_fetch), mock.patch.object(config, "STRUCTURE_OBS_BENCH_SYMBOL", "", create=True):
        _seed(db2)
        so.evaluate_pending(db2, now_ts=NOW + 30 * DAY)
    assert seen == ["TCS"] and db2.rows[0].bench_ret_20d is None


def test_an_explicit_stock_fetch_alone_does_not_trigger_a_benchmark_fetch():
    db = FakeDb()
    with _patched(db), mock.patch.object(config, "STRUCTURE_OBS_BENCH_SYMBOL", "NIFTY", create=True):
        _seed(db)
        so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=lambda s, lb: _bars(NOW, STOCK))   # the older tests call it this way
    assert db.rows[0].ret_20d == 20.0 and db.rows[0].bench_ret_20d is None


def test_report_shows_the_edge_with_the_market_taken_out():
    db = FakeDb()
    with _patched(db):
        for i in range(30):
            so.record(db, f"P{i}", "REAL", 100.0, VERDICT, SUMMARY, "enforce", False, NOW)
        for i in range(30):
            so.record(db, f"R{i}", "REAL", 100.0, dict(VERDICT, would_reject=True, reasons=["x"]), SUMMARY, "enforce", True, NOW)
        for r in db.rows:
            r.bars_seen, r.ret_10d, r.bench_ret_10d = 10, (6.0 if r.verdict == "passed" else 5.0), 5.0
        with mock.patch.object(so, "_since", lambda *a, **k: list(db.rows)):
            out = so.report(db, "REAL", days=60, recent=2, min_n=20, now_ts=NOW + DAY)
    st = out["stats"]
    assert st["edge"]["10d"]["edge"] == 1.0 and st["edge_excess"]["10d"]["edge"] == 1.0
    assert st["by_verdict"]["rejected"]["excess"]["10d"]["mean"] == 0.0 and st["by_verdict"]["passed"]["excess"]["10d"]["mean"] == 1.0
    assert all("bench_ret_10d" in row for row in out["recent"])
