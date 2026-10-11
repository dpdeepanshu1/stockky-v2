"""group309: board_log, board_gate (off/shadow/enforce, fail-open), board_report (stats + promotion rule)."""
from __future__ import annotations

import os
import queue
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import board_gate
from screening import board, board_log, board_report

T0 = 1_790_000_100.0 - (1_790_000_100.0 % 60)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    board_log.reset()
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "shadow", raising=False)
    monkeypatch.setattr(config, "BOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BOARD_CHASE_ATR", 1.5, raising=False)
    monkeypatch.setattr(board_log, "_ensure_thread", lambda: None)
    yield
    board_log.reset()


def ts_obj(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


def test_queue(monkeypatch):
    got = []
    monkeypatch.setattr(board_log, "_writer", lambda rows: got.extend(rows))
    assert board_log.enqueue({"symbol": "A"}) and board_log.enqueue({"symbol": "B"})
    assert board_log.flush() == 2 and [r["symbol"] for r in got] == ["A", "B"] and board_log.stats()["backlog"] == 0
    monkeypatch.setattr(board_log, "_q", queue.Queue(maxsize=2))
    assert board_log.enqueue({"a": 1}) and board_log.enqueue({"a": 2}) and board_log.enqueue({"a": 3}) is False
    assert board_log.stats()["dropped"] == 1


def test_writer_failure_counted(monkeypatch):
    def boom(rows):
        raise RuntimeError("db down")
    monkeypatch.setattr(board_log, "_writer", boom)
    board_log.enqueue({"symbol": "A"})
    assert board_log.flush() == 0 and board_log.stats()["write_errors"] == 1


def test_snapshot_rows_and_record(monkeypatch):
    payload = {"params_version": "b1", "bullish": [{"symbol": f"B{i}", "side": "bull", "rank": i + 1, "strength": 1.0, "conviction": 80,
               "ltp": 10, "open": 9.9, "vwap": 9.95, "extended": i == 0, "in_position": i == 1} for i in range(5)],
               "bearish": [{"symbol": "S1", "side": "bear", "rank": 1, "strength": 2.0, "conviction": 60, "ltp": 5, "open": 5.1}]}
    rows = board_log.snapshot_rows(payload, 3, T0)
    assert [r["symbol"] for r in rows] == ["B0", "B1", "B2", "S1"] and rows[0]["note"] == "extended" and rows[1]["in_position"] is True
    assert rows[0]["day"] == board_log._day_str(T0) and rows[0]["params_version"] == "b1"
    monkeypatch.setattr(config, "BOARD_LOG_TOP_N", 2, raising=False)
    assert board_log.record_snapshot(payload, T0) == 3


def bars_const(prices, start=T0):
    return [(start + 60 * i, p, p, p, p, 100) for i, p in enumerate(prices)]


def test_forward_returns():
    bars = bars_const([100] * 15 + [102] * 21)
    assert board_log.forward_pct(bars, T0, 100.0, "bull", 15, T0 + 3600) == pytest.approx(2.0)
    assert board_log.forward_pct(bars, T0, 100.0, "bear", 15, T0 + 3600) == pytest.approx(-2.0)
    assert board_log.forward_pct(bars, T0, 100.0, "bull", 15, T0 + 15 * 60 + 30) is None
    assert board_log.forward_pct(bars, T0, 0, "bull", 15, T0 + 3600) is None and board_log.forward_pct([], T0, 100.0, "bull", 15, T0 + 3600) is None
    assert board_log.forward_pct(bars_const([100] * 5), T0, 100.0, "bull", 30, T0 + 7200) is None
    b2 = bars_const([100] * 15 + [101] * 21)
    row = {"ts": ts_obj(T0), "side": "bull", "ltp": 100.0, "fwd15_pct": None, "fwd30_pct": 9.99, "fwd60_pct": None, "fwd_close_pct": None}
    assert board_log.forward_updates(row, b2, T0 + 20 * 60, None) == {"fwd15_pct": pytest.approx(1.0)}
    assert board_log.forward_updates({"ts": ts_obj(T0), "side": None, "ltp": 100.0}, b2, T0 + 9999, None) == {}


def test_close_pct():
    close_ts = T0 + 22500
    late = [(close_ts - 60 * k, 110.0, 110.0, 110.0, 110.0, 1) for k in range(10, 0, -1)]
    assert board_log.close_pct(late, 100.0, "bull", close_ts - 1, T0) is None
    assert board_log.close_pct(late, 100.0, "bull", close_ts + 5, T0) == pytest.approx(10.0)
    assert board_log.close_pct([(T0, 110.0, 110.0, 110.0, 110.0, 1)], 100.0, "bull", close_ts + 5, T0) is None


def test_fill_forward_real_sqlite(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    from feed import bar_store
    eng = create_engine("sqlite://")
    models.ScalpBoardLog.__table__.create(eng)
    S = sessionmaker(bind=eng)
    bar_store.reset()
    monkeypatch.setattr(config, "BOARD_MIN_DAY_VOLUME", 1, raising=False)
    day_open = 1_790_000_100.0
    for i in range(40):
        bar_store.on_tick("FWD", 100.0 + (2.0 if i >= 15 else 0.0), 1000 + i, day_open + 60 * i + 1)
    with S() as s:
        s.add(models.ScalpBoardLog(ts=ts_obj(day_open), day="d", kind="snapshot", symbol="FWD", side="bull", ltp=100.0, in_position=False))
        s.commit()
        assert board_log.fill_forward(s, "d", now=day_open + 3600) == 1
        r = s.query(models.ScalpBoardLog).one()
        assert r.fwd15_pct == pytest.approx(2.0) and r.fwd30_pct == pytest.approx(2.0) and r.fwd60_pct is None
        assert board_log.fill_forward(s, "d", now=day_open + 3601) == 0
    bar_store.reset()


def _facts(chase, status="fresh", rr=2.0):
    return {"chase_atr": chase, "zone_status": status, "rr": rr, "extended": chase is not None and chase > config.BOARD_CHASE_ATR}


def test_gate_modes(monkeypatch):
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "off", raising=False)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: (_ for _ in ()).throw(AssertionError("must not run")))
    assert board_gate.check("A", 100.0) is None and board_log.stats()["queued"] == 0
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "shadow", raising=False)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(2.4))
    assert board_gate.check("A", 100.0) is None
    row = board_log._q.get_nowait()
    assert row["kind"] == "entry" and row["note"] == "would_block:extended" and row["chase_atr"] == 2.4 and row["side"] == "bull"
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(0.3)); board_gate.check("A", 100.0)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: None); board_gate.check("A", 100.0)
    assert [board_log._q.get_nowait()["note"] for _ in range(2)] == ["pass", "no_zone_data"]
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "enforce", raising=False)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(2.4))
    r = board_gate.check("A", 100.0)
    assert r and r.startswith("BOARD_CHASE:") and board_log._q.get_nowait()["note"] == "enforced:extended"
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(0.9))
    assert board_gate.check("A", 100.0) is None
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: None)
    assert board_gate.check("A", 100.0) is None
    monkeypatch.setattr(config, "BOARD_ENABLED", False, raising=False)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(9.0))
    assert board_gate.check("A", 100.0) is None


def test_gate_fails_open(monkeypatch):
    monkeypatch.setattr(config, "BOARD_GATE_MODE", "enforce", raising=False)
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert board_gate.check("A", 100.0) is None
    monkeypatch.setattr(board_gate, "evaluate", lambda *a: _facts(5.0))
    monkeypatch.setattr(board_gate, "_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log boom")))
    assert board_gate.check("A", 100.0) is None


def test_evaluate_and_entry_wiring(monkeypatch):
    zone = {"status": "fresh", "top": 100.0, "bottom": 99.0, "atr": 0.5}
    monkeypatch.setattr(board, "zone_view", lambda s, side, ltp, *a, **k: {"zone": zone, "chase_atr": 2.0, "rr": {"rr": 1.8}})
    assert board_gate.evaluate("A", 101.0) == {"chase_atr": 2.0, "zone_status": "fresh", "rr": 1.8, "extended": True}
    monkeypatch.setattr(board, "zone_view", lambda *a, **k: None)
    assert board_gate.evaluate("A", 101.0) is None
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py")).read()
    assert src.index("depth_gate.reject_reason(") < src.index("board_gate.check(") < src.index("ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct)")


def _row(kind, day, sym, side="bull", rank=1, conv=80, note=None, zone="fresh", f30=None, ts=0, ver="b1"):
    return {"kind": kind, "day": day, "symbol": sym, "side": side, "rank": rank, "conviction": conv, "note": note, "zone_status": zone,
            "fwd30_pct": f30, "fwd15_pct": None, "fwd60_pct": None, "fwd_close_pct": None, "ts": ts, "params_version": ver}


def test_report_pieces():
    rows = [_row("snapshot", "d1", "A", ts=2, f30=1), _row("snapshot", "d1", "A", ts=1, f30=2), _row("snapshot", "d2", "A", ts=1),
            _row("snapshot", "d1", "A", side="bear", ts=1)]
    out = board_report.dedupe(rows)
    assert len(out) == 3 and [r["fwd30_pct"] for r in out if r["day"] == "d1" and r["side"] == "bull"] == [2]
    a, b = [1.0, 1.2, 0.9, 1.1] * 10, [0.0, 0.1, -0.1, 0.05] * 10
    ci = board_report.bootstrap_diff(a, b)
    assert ci == board_report.bootstrap_diff(a, b) and ci[0] > 0.5 and board_report.bootstrap_diff([], b) is None
    big = {"n": 40, "sessions": 12, "mean_net": 0.3, "win_rate": 0.6}
    ctl = {"n": 40, "sessions": 12, "mean_net": 0.0, "win_rate": 0.5}
    v = board_report.verdict
    assert v(big, ctl, (0.05, 0.4), 30, 10) == "promote" and v(big, ctl, (-0.05, 0.4), 30, 10) == "do not promote"
    assert v(dict(big, mean_net=-0.1), ctl, (0.05, 0.4), 30, 10) == "do not promote"
    assert v(dict(big, n=10), ctl, (0.05, 0.4), 30, 10).startswith("needs more data (10/30 obs")
    assert v(dict(big, sessions=3), ctl, (0.05, 0.4), 30, 10).startswith("needs more data")
    assert "no control" in v(big, ctl, None, 30, 10)
    assert v(dict(big, mean_net=-0.5), ctl, (-0.8, -0.2), 30, 10, invert=True) == "promote"
    assert v(big, ctl, (-0.1, 0.3), 30, 10, invert=True) == "do not promote"


def test_report_end_to_end():
    few = [_row("entry", "d1", "A", note="would_block:extended", f30=-1.0), _row("entry", "d1", "B", note="pass", f30=0.4)]
    assert board_report.report(few, 0.12, 30, 10, "b1")["rules"]["chase_guard"]["verdict"].startswith("needs more data (1/30 obs, 1/10 sessions)")
    many = []
    for d in range(12):
        for i in range(4):
            many.append(_row("entry", f"d{d}", f"X{i}", note="would_block:extended", f30=-1.0 + 0.1 * (i % 2)))
            many.append(_row("entry", f"d{d}", f"Y{i}", note="pass", f30=0.4 + 0.1 * (i % 2)))
    r = board_report.report(many, 0.12, 30, 10, "b1")["rules"]["chase_guard"]
    assert r["verdict"] == "promote" and r["horizons"]["30m"]["signal"]["n"] == 48 and r["would_switch_on"] == "BOARD_GATE_MODE=enforce"
    snaps = []
    for d in range(2):
        for k in range(5):
            snaps.append(_row("snapshot", f"d{d}", "TOP", rank=1, f30=1.0, ts=k)); snaps.append(_row("snapshot", f"d{d}", "MID", rank=10, f30=0.0, ts=k))
    snaps.append(_row("snapshot", "d0", "OLD", rank=1, f30=9.0, ver="b0"))
    sig = board_report.report(snaps, 0.0, 30, 10, "b1")["rules"]["top5_rank"]["horizons"]["30m"]["signal"]
    assert sig["n"] == 2 and sig["mean_net"] == 1.0
    assert board_report.report([_row("snapshot", "d1", "A", rank=1, f30=0.1)], 0.12, 30, 10)["rules"]["top5_rank"]["horizons"]["30m"]["signal"]["mean_net"] == pytest.approx(-0.02)
    e = board_report.report([], 0.12, 30, 10)
    assert e["rows"] == 0 and all(x["verdict"].startswith("needs more data") for x in e["rules"].values())
