"""group 356 (WS-6): real-trade structure observation - recording, de-duplication, the evaluator pass and the edge report.
Runs with pytest or plain python; the SQLAlchemy touch points (_model/_exists/_pending/_since) are replaced by in-memory fakes, and one
real-SQLite round trip (skipped when sqlalchemy is missing) checks those four queries against the actual table."""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import structure_observation as so

DAY = 86400.0
NOW = 1_700_000_000.0 + 6 * 3600          # a trading-hours stamp


class FakeRow:
    def __init__(self, **kw):
        self.evaluated_at = None
        self.id = None
        for k in ("ret_1d", "ret_5d", "ret_10d", "ret_20d", "mfe_pct", "mae_pct", "first_touch"):
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
    st.enter_context(mock.patch.object(so, "_since", lambda d, m, t, limit=5000: sorted([r for r in d.rows if r.mode == m and r.obs_ts >= t], key=lambda r: -r.obs_ts)))
    st.enter_context(mock.patch.object(config, "STRUCTURE_OBS_ENABLED", True, create=True))
    so.reset()
    return st


def _v(opinion="ok", reject=False, reasons=()):
    return {"opinion": opinion, "would_reject": reject, "reasons": list(reasons)}


SUMMARY = {"ms_quality": "ok", "ms_stop": 95.0, "ms_target": 110.0, "ms_atr": 2.0, "ms_tqs": 61, "ms_trend": "bullish"}


def test_record_stores_passed_and_rejected_but_not_no_opinion():
    db = FakeDb()
    with _patched(db):
        assert so.record(db, "tcs", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW) is True
        assert so.record(db, "infy", "REAL", 100.0, _v(reject=True, reasons=["chase 2.0 ATR"]), SUMMARY, "shadow", False, NOW) is True
        assert so.record(db, "wipro", "REAL", 100.0, _v(reject=True, reasons=["x"]), SUMMARY, "enforce", True, NOW) is True
        assert so.record(db, "itc", "REAL", 100.0, _v("no_opinion"), None, "enforce", False, NOW) is False
    assert [r.verdict for r in db.rows] == ["passed", "rejected_shadow", "rejected_enforced"]
    r = db.rows[0]
    assert r.symbol == "TCS" and r.mode == "REAL" and r.stop == 95.0 and r.target == 110.0 and r.atr == 2.0 and r.is_complete is False
    assert not hasattr(r, "complete") and "evaluated_at" in r.__dict__ and r.evaluated_at is None
    assert json.loads(db.rows[1].reasons_json) == ["chase 2.0 ATR"]
    st = so.stats()
    assert st["recorded"] == 3 and st["skipped_no_opinion"] == 1


def test_one_row_per_symbol_mode_day_first_verdict_wins():
    db = FakeDb()
    with _patched(db):
        assert so.record(db, "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW)
        assert not so.record(db, "TCS", "REAL", 103.0, _v(reject=True, reasons=["x"]), SUMMARY, "enforce", True, NOW + 600)
        assert so.record(db, "TCS", "DEMO", 100.0, _v(), SUMMARY, "enforce", False, NOW)           # other mode: its own row
        assert so.record(db, "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW + DAY)     # next day: its own row
        so._seen.clear()                                                                            # a restart: the DB check still dedupes
        assert not so.record(db, "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW)
    assert len(db.rows) == 3 and so.stats()["skipped_dup"] == 2


def test_record_never_raises_and_respects_the_switch():
    class Boom(FakeDb):
        def add(self, r):
            raise RuntimeError("db down")
    with _patched(Boom()):
        assert so.record(Boom(), "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW) is False
        assert so.stats()["errors"] == 1
    with _patched(FakeDb()), mock.patch.object(config, "STRUCTURE_OBS_ENABLED", False, create=True):
        assert so.record(FakeDb(), "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW) is False
    with _patched(FakeDb()):
        assert so.record(FakeDb(), "TCS", "REAL", 0.0, _v(), SUMMARY, "enforce", False, NOW) is False       # no price
        assert so.record(FakeDb(), "", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW) is False


def _bars(obs_ts, closes):
    from stockky_ms.candles import day_key
    k = day_key(obs_ts)
    return [(float((k + 1 + i) * 86400), c, c + 1.0, c - 1.0, c, 1000.0) for i, c in enumerate(closes)]


def _seed(db, symbol="TCS", verdict="passed", ts=NOW):
    so.record(db, symbol, "REAL", 100.0, _v(reject=verdict != "passed", reasons=["x"]), SUMMARY, "enforce" if verdict == "rejected_enforced" else "shadow",
              verdict == "rejected_enforced", ts)


def test_evaluate_fills_outcomes_and_completes_after_20_sessions():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        fetched = []
        def fetch(sym, lookback):
            fetched.append((sym, lookback))
            return _bars(NOW, [101 + i for i in range(25)])
        res = so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=fetch)
    r = db.rows[0]
    assert res["updated"] == 1 and res["completed"] == 1 and db.commits == 1 and fetched[0][0] == "TCS" and fetched[0][1] >= 20
    assert (r.ret_1d, r.ret_5d, r.ret_10d, r.ret_20d) == (1.0, 5.0, 10.0, 20.0) and r.bars_seen == 20 and r.is_complete is True and r.evaluated_at is not None


def test_evaluate_partial_then_later_pass_and_recent_skip():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        calls = []
        def fetch(sym, lb):
            calls.append(sym)
            return _bars(NOW, [101, 102, 103])
        so.evaluate_pending(db, now_ts=NOW + 4 * DAY, fetch=fetch)
        r = db.rows[0]
        assert r.bars_seen == 3 and r.is_complete is False and r.ret_1d == 1.0 and r.ret_5d is None
        so.evaluate_pending(db, now_ts=NOW + 4 * DAY + 3600, fetch=fetch)         # evaluated an hour ago: no second fetch
        assert len(calls) == 1
        r.evaluated_at = r.evaluated_at.replace(year=2000)                          # long ago
        so.evaluate_pending(db, now_ts=NOW + 5 * DAY, fetch=lambda s, lb: _bars(NOW, [101, 102, 103, 104]))
        assert r.bars_seen == 4


def test_same_day_rows_are_not_fetched_and_failures_retry():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        def never(sym, lb):
            raise AssertionError("must not fetch")
        assert so.evaluate_pending(db, now_ts=NOW + 600, fetch=never)["symbols"] == 0
        def down(sym, lb):
            raise RuntimeError("history down")
        res = so.evaluate_pending(db, now_ts=NOW + 2 * DAY, fetch=down)
        assert res["fetch_fail"] == 1 and res["updated"] == 0 and db.rows[0].is_complete is False and db.commits == 0


def test_rows_with_no_history_after_the_give_up_window_are_closed():
    db = FakeDb()
    with _patched(db):
        _seed(db)
        res = so.evaluate_pending(db, now_ts=NOW + (config.STRUCTURE_OBS_GIVE_UP_DAYS + 2) * DAY, fetch=lambda s, lb: [])
    assert res["gave_up"] == 1 and db.rows[0].is_complete is True and db.rows[0].bars_seen == 0


def test_report_aggregates_rejected_against_passed():
    db = FakeDb()
    with _patched(db):
        for i in range(25):
            _seed(db, f"P{i}", "passed", NOW - i * 60)
            _seed(db, f"R{i}", "rejected_shadow", NOW - i * 60)
        for r in db.rows:
            ok = r.verdict == "passed"
            r.ret_10d, r.bars_seen, r.mfe_pct, r.mae_pct, r.first_touch = (2.0 if ok else -2.0), 10, 3.0, -1.0, ("target" if ok else "stop")
        out = so.report(db, "real", days=30, recent=5, min_n=20, now_ts=NOW + DAY)
    assert out["mode"] == "REAL" and out["stats"]["conclusion"] == "gate_helps" and out["stats"]["edge"]["10d"]["edge"] == 4.0
    assert out["stats"]["by_verdict"]["rejected"]["n"] == 25 and len(out["recent"]) == 5 and out["recent"][0]["reasons"] == ["x"]
    with _patched(FakeDb()):
        empty = so.report(FakeDb(), "REAL")
    assert empty["stats"]["total"] == 0 and empty["stats"]["conclusion"] == "insufficient_sample"


def test_real_sqlite_round_trip_when_sqlalchemy_is_available():
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import models
    except Exception:  # noqa: BLE001
        return
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    so.reset()
    with mock.patch.object(config, "STRUCTURE_OBS_ENABLED", True, create=True):
        assert so.record(db, "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW)
        db.commit()
        so._seen.clear()
        assert not so.record(db, "TCS", "REAL", 100.0, _v(), SUMMARY, "enforce", False, NOW)
        res = so.evaluate_pending(db, now_ts=NOW + 30 * DAY, fetch=lambda s, lb: _bars(NOW, [101 + i for i in range(25)]))
        assert res["completed"] == 1
        out = so.report(db, "REAL", days=365, now_ts=NOW + 40 * DAY)
    assert out["stats"]["total"] == 1 and out["stats"]["complete"] == 1 and out["recent"][0]["ret_20d"] == 20.0
