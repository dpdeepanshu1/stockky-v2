"""group309: board_runner.run_once + the /board routes over real HTTP (FastAPI TestClient, in-memory sqlite)."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import models
import main as m
import feed.ws_client as wsc
from feed import bar_store as bs
from screening import board, board_log, board_runner, prev_day
from tz_utils import ist_today_str

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 10, 12, 9, 15, tzinfo=IST).timestamp()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    board_runner.reset(); board_log.reset(); board.reset(); bs.reset(); prev_day.reset(); wsc._day_stats.clear()
    monkeypatch.setattr(config, "BOARD_LOG_EVERY_S", 300, raising=False)
    monkeypatch.setattr(config, "BOARD_LOG_TOP_N", 20, raising=False)
    monkeypatch.setattr(board_log, "_ensure_thread", lambda: None)
    yield
    board_runner.reset(); board_log.reset(); board.reset(); bs.reset(); prev_day.reset(); wsc._day_stats.clear()


def _payload(n=3):
    return {"bullish": [{"symbol": f"S{i}", "side": "bull", "rank": i + 1, "strength": 1.0, "conviction": 70, "ltp": 10.0, "open": 9.9} for i in range(n)],
            "bearish": [], "params_version": "b1"}


def test_fill_window():
    assert board_runner.in_fill_window(OPEN, True)
    assert board_runner.in_fill_window(datetime(2026, 10, 12, 15, 45, tzinfo=IST).timestamp(), False)
    assert not board_runner.in_fill_window(datetime(2026, 10, 12, 17, 0, tzinfo=IST).timestamp(), False)
    assert not board_runner.in_fill_window(datetime(2026, 10, 12, 8, 0, tzinfo=IST).timestamp(), False)
    assert not board_runner.in_fill_window(datetime(2026, 10, 10, 16, 0, tzinfo=IST).timestamp(), False)


def test_runner_cadence_empty_closed_and_failures(monkeypatch):
    queued = []
    monkeypatch.setattr(board, "build", lambda **k: _payload())
    monkeypatch.setattr(board_log, "record_snapshot", lambda p, now: queued.append(now) or 3)
    monkeypatch.setattr(bs, "prune", lambda: 4)
    d1 = board_runner.run_once(OPEN + 1000, True, open_symbols_fn=lambda: ["X"], session_factory=None, today_fn=lambda: "2026-10-12")
    assert d1["snapshot"] is True and d1["pruned"] == 4
    assert board_runner.run_once(OPEN + 1100, True, open_symbols_fn=lambda: [], session_factory=None)["snapshot"] is False
    assert board_runner.run_once(OPEN + 1301, True, open_symbols_fn=lambda: [], session_factory=None)["snapshot"] is True and len(queued) == 2
    board_runner.reset()
    monkeypatch.setattr(board, "build", lambda **k: {"bullish": [], "bearish": []})
    assert board_runner.run_once(OPEN + 1000, True, open_symbols_fn=lambda: [])["snapshot"] is False
    board_runner.reset()
    after = datetime(2026, 10, 12, 15, 45, tzinfo=IST).timestamp()
    monkeypatch.setattr(board, "build", lambda **k: (_ for _ in ()).throw(AssertionError("closed market must not snapshot")))
    calls = []

    class Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(board_log, "fill_forward", lambda s, day, now: calls.append(day) or 7)
    d = board_runner.run_once(after, False, session_factory=lambda: Sess(), today_fn=lambda: "2026-10-12")
    assert d["snapshot"] is False and d["fill"] == 7 and calls == ["2026-10-12"]
    assert board_runner.run_once(after + 30, False, session_factory=lambda: Sess(), today_fn=lambda: "x")["fill"] == 0 and len(calls) == 1
    board_runner.reset()
    monkeypatch.setattr(board, "build", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = board_runner.run_once(OPEN + 1000, True, open_symbols_fn=lambda: [])
    assert d["snapshot"] is False and board_runner.state()["errors"] == 1 and "boom" in board_runner.state()["last_error"]
    board_runner.reset()
    monkeypatch.setattr(board, "build", lambda **k: _payload())
    monkeypatch.setattr(board_log, "record_snapshot", lambda p, now: 1)
    monkeypatch.setattr(bs, "prune", lambda: 0)
    def bad():
        raise RuntimeError("db down")
    assert board_runner.run_once(OPEN + 1000, True, open_symbols_fn=bad)["snapshot"] is True


SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()


def test_static_checks_on_main():
    assert re.findall(r'@app\.get\("(/board[^"]*)"\)', SRC) == ["/board/status", "/board/intraday/{symbol}", "/board/self-check", "/board/shadow-report", "/board", "/board/{symbol}"]
    block = SRC[SRC.index('@app.get("/board/status")'):SRC.index("def board_symbol")]
    assert "require_admin" not in block and "@app.post" not in block and ".commit(" not in block
    assert "_board_runner.loop()" in SRC and "if config.BOARD_ENABLED:" in SRC and SRC.count("_board_task.cancel()") == 1


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


@pytest.fixture()
def client(db, monkeypatch):
    m.app.dependency_overrides[m.get_db] = lambda: db
    monkeypatch.setattr(config, "BOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BOARD_CACHE_S", 0.0, raising=False)
    monkeypatch.setattr(config, "BOARD_MIN_DAY_VOLUME", 1000, raising=False)
    monkeypatch.setattr(m, "_board_open_symbols", lambda: [])
    yield TestClient(m.app)
    m.app.dependency_overrides.clear()


def _feed(sym, a, b, n=30):
    for i in range(n):
        bs.on_tick(sym, a + (b - a) * i / (n - 1), 10_000 + 1000 * i, OPEN + 60 * i + 5)
    wsc._day_stats[sym] = (a, max(a, b), min(a, b) - 0.01, a - 1)
    prev_day._cache[sym] = (ist_today_str(), prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-09", 2.0))


def test_http_board_rows_params_and_disabled(client, monkeypatch):
    _feed("AAA", 100, 104); _feed("BBB", 100, 97); _feed("RELIANCE", 100, 103)
    j = client.get("/board").json()
    assert [x["symbol"] for x in j["bullish"]] == ["AAA", "RELIANCE"] and [x["symbol"] for x in j["bearish"]] == ["BBB"] and j["stale"] is False
    assert [x["symbol"] for x in client.get("/board?universe=fno").json()["bullish"]] == ["RELIANCE"]
    assert len(client.get("/board?limit=5").json()["bullish"]) == 2
    st = client.get("/board/status").json()
    assert st["enabled"] is True and "bar_store" in st and "log" in st and "runner" in st
    monkeypatch.setattr(config, "BOARD_ENABLED", False, raising=False)
    assert client.get("/board").status_code == 404 and client.get("/board/AAA").status_code == 404


def test_http_stale_fallback_and_empty(client, db):
    assert client.get("/board").json()["bullish"] == []
    now = datetime.now(timezone.utc)
    for sym, side, rank in (("OLD1", "bull", 1), ("OLD2", "bull", 2), ("OLDB", "bear", 1)):
        db.add(models.ScalpBoardLog(ts=now, day="2026-10-09", kind="snapshot", symbol=sym, side=side, rank=rank, strength=1.0,
                                    conviction=70, ltp=10.0, day_open=9.9, in_position=False))
    db.commit()
    j = client.get("/board").json()
    assert j["stale"] is True and j["stale_day"] == "2026-10-09" and [x["symbol"] for x in j["bullish"]] == ["OLD1", "OLD2"]


def test_http_symbol_overlay_and_shadow_report(client, db):
    assert client.get("/board/ZZZ").status_code == 404
    _feed("HELD", 100, 103, n=40)
    db.add(models.ScalpPosition(symbol="HELD", dhan_security_id="1", window_source="5m", status="OPEN", entry_price=101.0, quantity=10,
                                target_price=104.0, stop_price=100.0, adaptive_target_pct=3.0, adaptive_stop_pct=1.0, capital_risked=10.0))
    today = datetime.now(IST).strftime("%Y-%m-%d")
    db.add(models.ScalpBoardLog(ts=datetime.now(timezone.utc), day=today, kind="entry", symbol="X", side="bull", note="pass", ltp=10.0,
                                in_position=False, params_version=config.BOARD_PARAMS_VERSION, fwd30_pct=0.2))
    db.commit()
    j = client.get("/board/held").json()
    assert j["position"]["stop"] == 100.0 and j["position"]["target"] == 104.0 and len(j["candles"]) >= 5
    r = client.get("/board/shadow-report?days=7").json()
    assert r["rows"] == 1 and r["days"] == 7 and r["rules"]["chase_guard"]["verdict"].startswith("needs more data")
