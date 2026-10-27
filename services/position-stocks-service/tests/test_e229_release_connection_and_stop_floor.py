"""E229: connection released before slow network waits in _run_cycle; stop-vs-ATR floor also applies after the opening window."""
import re
from datetime import datetime
from pathlib import Path
from unittest import mock

SRC = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")


def test_release_before_quality_gather_and_market_gate():
    assert SRC.index("_quality_cache = quality_gate.get_cache_batch(db,") < SRC.index('_release_db_connection(db, "quality gate")') < SRC.index("quality_results = await asyncio.gather(")
    assert SRC.index('_release_db_connection(db, "market gate")') < SRC.index("_mkt = await trade_gates.market_gate_reject()")


def test_release_helper_never_raises_and_pool_logged():
    body = SRC[SRC.index("def _release_db_connection("):SRC.index("def _pool_status_text(")]
    assert "db.commit()" in body and "db.rollback()" in body and not re.search(r"^\s+raise\b", body, re.M)
    assert "DB pool exhausted: %s\", _pool_status_text()" in SRC


def test_stop_floor_after_window():
    import config
    from screening import opening_gate as og, prev_day
    pdv = prev_day.PrevDay(high=110, low=90, close=100, atr=7.76, date="2026-10-26")
    with mock.patch.object(og, "is_active", return_value=False), mock.patch.object(prev_day, "get", return_value=pdv):
        with mock.patch.object(config, "STOP_ATR_FLOOR_AFTER_WINDOW", True):
            r = og.stop_reject("X", 2.0)
            assert r and r.startswith("STOP_ATR_FLOOR:STOP_TOO_TIGHT")
            assert og.stop_reject("X", 2.5) is None
        with mock.patch.object(config, "STOP_ATR_FLOOR_AFTER_WINDOW", False):
            assert og.stop_reject("X", 2.0) is None
    with mock.patch.object(og, "is_active", return_value=False), mock.patch.object(prev_day, "get", return_value=None):
        assert og.stop_reject("X", 2.0) is None   # fail open after the window
    with mock.patch.object(og, "is_active", return_value=True), mock.patch.object(prev_day, "get", return_value=None):
        assert og.stop_reject("X", 2.0) == "OPENING_GATE:NO_ATR_DATA"   # still fail closed inside it
