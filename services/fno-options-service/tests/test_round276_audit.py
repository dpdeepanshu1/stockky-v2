"""Round 276 audit fixes: the open list is filtered before LIMIT, the in-service backtests get the holiday list, route inputs are validated, the CLI defaults to the official holidays."""
from __future__ import annotations

import pytest

import config
from fno import paper
from fno.params import Params
from fno.store import Store


def _insert(store, n_closed, ts0=1_700_000_000.0):
    from fno.paper import Position
    ids = []
    for i in range(n_closed + 1):
        p = Position(underlying="NIFTY", expiry="2026-11-17", strike=24500.0 + i, opt_type="CE", lot_size=65, lots=1, entry_ts=ts0 + i * 60, entry_premium=100.0, sl_price=70.0, target_price=150.0)
        if i > 0:                                   # every row after the first (oldest) is closed
            paper.close_position(p, 110.0, ts0 + i * 60 + 30, paper.TARGET, Params())
        store.insert_position(p)
        ids.append(p.id)
    return ids


def test_store_status_filter_applies_before_limit():
    s = Store(":memory:")
    _insert(s, 5)                                    # the oldest row is the only OPEN one, followed by 5 newer CLOSED rows
    assert len(s.positions(limit=3)) == 3 and all(p.status == "CLOSED" for p in s.positions(limit=3))     # the old behaviour: the open row fell off
    got = s.positions(limit=3, status="OPEN")
    assert len(got) == 1 and got[0].status == "OPEN"
    assert len(s.positions(limit=10, status="CLOSED")) == 5


def test_cli_default_holidays_is_the_official_list():
    from backtest import cli
    assert cli._default_holidays().split(",") == list(config.HOLIDAYS)
    ap = __import__("argparse").ArgumentParser()
    cli._common(ap)
    assert ap.parse_args([]).holidays.split(",") == list(config.HOLIDAYS)
    assert ap.parse_args(["--holidays", ""]).holidays == ""          # an explicit empty value still means none


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    yield TestClient(main.app), main
    main.app.dependency_overrides.clear()


def test_positions_route_open_filter_sees_an_old_open_row(client):
    c, main = client
    _insert(main.store, 5)
    r = c.get("/fno/positions?status=open&limit=2").json()["positions"]
    assert len(r) == 1 and r[0]["status"] == "OPEN"


def test_route_inputs_are_validated(client):
    c, main = client
    assert c.get("/fno/expiries?underlying=FOO").status_code == 400
    assert c.get("/fno/chain?underlying=NIFTY&expiry=../../x").status_code == 400
    assert c.post("/fno/backtests/run", json={"kind": "replay", "underlyings": ["NIFTY"], "days": ["../../etc"]}).status_code == 400


def test_backtest_jobs_pass_the_holiday_list(client, monkeypatch):
    c, main = client
    seen = {}
    monkeypatch.setattr(main.bt_sim, "run", lambda *a, **k: seen.update(sim=k.get("holidays")) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(main.bt_replay, "run", lambda *a, **k: seen.update(replay=a[5] if len(a) > 5 else k.get("holidays")) or (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(main.provider, "daily", lambda u: [])
    monkeypatch.setattr(main.provider, "intraday", lambda u: [])
    monkeypatch.setattr(main.provider, "intraday_history", lambda u: [], raising=False)
    main._jobs["a"] = {"id": "a", "state": "running"}
    main._run_job("a", main.BacktestBody(kind="sim", underlyings=["NIFTY"], strategy="CORE"))
    main._run_job("a", main.BacktestBody(kind="replay", underlyings=["NIFTY"], days=["2026-11-09"]))
    assert seen["sim"] == list(config.HOLIDAYS) and seen["replay"] == list(config.HOLIDAYS)
