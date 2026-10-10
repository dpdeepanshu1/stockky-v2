"""group292: end-of-day report against real in-memory SQLite and the real HTTP routes.

Covers what test_group292_daily_report.py fakes: the snapshot really stored in trade_resilience_cache and read back,
history listing, the schedule tick calling the step (armed or not, with or without a gate row), and the three routes."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import trade_records as tr
from execution import auto_pilot as ap
from test_group290_trade_records import seed_trade
from tz_utils import ist_now, ist_today_str


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _closed_day(position) -> str:
    """IST day the seeded position closed on (seed_trade closes it 60 minutes ago by default)."""
    return ist_now(position.closed_at.replace(tzinfo=timezone.utc)).strftime("%Y-%m-%d")


# ── real snapshot round trip ───────────────────────────────────────────────────────────────────────────────────────

def test_snapshot_of_a_real_closed_trade_is_stored_and_read_back(db):
    p = seed_trade(db, "AAA", mode="REAL", pnl=48.0, charges=(10.0, 12.5))
    day = _closed_day(p)
    snap = tr.build_daily_snapshot(db, "REAL", day)
    assert snap["today"]["overall"]["trades"] == 1 and snap["today"]["overall"]["net_pnl"] == 25.5
    assert tr.save_daily_snapshot(db, snap) is True
    back = tr.load_daily_snapshot(db, "REAL", day)
    assert back["day"] == day and back["today"]["overall"]["net_pnl"] == 25.5
    assert back["records_today"][0]["symbol"] == "AAA"
    row = db.query(models.ResilienceCache).filter_by(key=f"daily_report:REAL:{day}").one()
    assert json.loads(row.payload_json)["mode"] == "REAL"


def test_other_mode_and_other_day_do_not_leak_into_a_snapshot(db):
    p = seed_trade(db, "AAA", mode="REAL")
    seed_trade(db, "BBB", mode="DEMO")
    day = _closed_day(p)
    assert tr.build_daily_snapshot(db, "REAL", day)["today"]["overall"]["trades"] == 1
    assert tr.build_daily_snapshot(db, "REAL", "2001-01-01")["today"]["overall"] is None
    assert tr.build_daily_snapshot(db, "REAL", "2001-01-01")["week"]["overall"]["trades"] == 1


def test_saving_twice_overwrites_instead_of_duplicating(db):
    snap = {"mode": "DEMO", "day": "2026-10-09", "today": {"overall": {"trades": 1}}, "generated_at": "a"}
    assert tr.save_daily_snapshot(db, snap)
    snap2 = dict(snap, generated_at="b")
    assert tr.save_daily_snapshot(db, snap2)
    assert db.query(models.ResilienceCache).filter_by(key="daily_report:DEMO:2026-10-09").count() == 1
    assert tr.load_daily_snapshot(db, "DEMO", "2026-10-09")["generated_at"] == "b"


# ── history listing ────────────────────────────────────────────────────────────────────────────────────────────────

def _store(db, mode, day, trades=1, net=10.0):
    assert tr.save_daily_snapshot(db, {"mode": mode, "day": day, "generated_at": f"t-{day}",
                                       "today": {"overall": {"trades": trades, "net_pnl": net, "expectancy": net}}})


def test_history_is_newest_first_and_per_mode(db):
    for d in ("2026-10-07", "2026-10-09", "2026-10-08"):
        _store(db, "REAL", d, net=float(d[-1]))
    _store(db, "DEMO", "2026-10-09")
    rows = tr.list_daily_snapshots(db, "REAL")
    assert [r["day"] for r in rows] == ["2026-10-09", "2026-10-08", "2026-10-07"]
    assert rows[0]["trades"] == 1 and rows[0]["net_pnl"] == 9.0 and rows[0]["generated_at"] == "t-2026-10-09"
    assert [r["day"] for r in tr.list_daily_snapshots(db, "DEMO")] == ["2026-10-09"]


def test_history_limit_and_empty(db):
    assert tr.list_daily_snapshots(db, "REAL") == []
    for i in range(1, 6):
        _store(db, "REAL", f"2026-10-0{i}")
    assert len(tr.list_daily_snapshots(db, "REAL", 2)) == 2
    assert len(tr.list_daily_snapshots(db, "REAL", 0)) == 1            # clamped to 1


def test_history_ignores_lookalike_keys_and_bad_json(db):
    _store(db, "REAL", "2026-10-09")
    # "_" is a LIKE wildcard: this key matches the SQL pattern but is not a daily_report key
    db.add(models.ResilienceCache(key="dailyXreport:REAL:2026-10-10", payload_json=json.dumps({"day": "x"})))
    db.add(models.ResilienceCache(key="daily_report:REAL:2026-10-11", payload_json="{not json"))
    db.commit()
    assert [r["day"] for r in tr.list_daily_snapshots(db, "REAL")] == ["2026-10-09"]


# ── the schedule tick drives the step ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def tick(engine, monkeypatch):
    factory = sessionmaker(bind=engine)
    pushed = []

    async def notify(text):
        pushed.append(text)
        return True
    monkeypatch.setattr(ap, "get_session_factory", lambda: factory)
    monkeypatch.setattr(ap, "notify_async", notify)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", True, raising=False)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_TIME_IST", "00:00", raising=False)
    monkeypatch.setattr(ap, "is_ist_weekday", lambda *a, **k: True)
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: True)
    monkeypatch.setattr(ap, "is_market_open_ist", lambda *a, **k: False)
    monkeypatch.setattr(ap, "_edis_check_enabled", lambda *a, **k: False)
    ap._daily_report_last_failure.clear()
    yield {"factory": factory, "pushed": pushed}
    ap._daily_report_last_failure.clear()


def _seed_today_trade(factory):
    s = factory()
    try:
        # closed 1 minute ago so it always falls on today's IST date, whatever time the suite runs
        seed_trade(s, "AAA", mode="REAL", opened_min=30, closed_min=1)
    finally:
        s.close()


def test_tick_stores_and_pushes_for_an_unarmed_mode(tick):
    s = tick["factory"]()
    s.add(models.TradeGateState(mode="REAL", armed=False))
    s.commit()
    s.close()
    _seed_today_trade(tick["factory"])
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        snap = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert snap is not None and snap["today"]["overall"]["trades"] == 1
    assert len(tick["pushed"]) == 1 and "REAL" in tick["pushed"][0]


def test_tick_runs_the_report_even_without_a_gate_row(tick):
    _seed_today_trade(tick["factory"])
    asyncio.run(ap._schedule_tick_body("REAL"))
    assert len(tick["pushed"]) == 1


def test_a_second_tick_does_not_send_again(tick):
    _seed_today_trade(tick["factory"])
    asyncio.run(ap._schedule_tick_body("REAL"))
    asyncio.run(ap._schedule_tick_body("REAL"))
    assert len(tick["pushed"]) == 1


def test_tick_with_no_trades_stores_an_empty_snapshot_and_stays_quiet(tick):
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        snap = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert snap is not None and snap["today"]["overall"] is None and tick["pushed"] == []


def test_tick_does_nothing_when_the_report_is_disabled(tick, monkeypatch):
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", False)
    _seed_today_trade(tick["factory"])
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        assert tr.load_daily_snapshot(s, "REAL", ist_today_str()) is None
    finally:
        s.close()
    assert tick["pushed"] == []


def test_a_database_failure_in_the_step_never_breaks_the_tick(tick, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(tr, "build_daily_snapshot", boom)
    asyncio.run(ap._schedule_tick_body("REAL"))          # must not raise
    assert tick["pushed"] == []


# ── HTTP routes ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    pushed = []

    async def notify(text):
        pushed.append(text)
        return True
    monkeypatch.setattr(ap, "notify_async", notify)
    monkeypatch.setattr(ap.config, "DAILY_REPORT_ENABLED", True, raising=False)
    main.app.dependency_overrides[main.get_db] = lambda: db
    c = TestClient(main.app)
    c.pushed = pushed
    yield c
    main.app.dependency_overrides.clear()


def test_daily_route_is_404_until_a_report_is_stored(client):
    assert client.get("/positions/DEMO/report/daily").status_code == 404
    assert client.get("/positions/DEMO/report/daily", params={"day": "2026-10-09"}).status_code == 404


@pytest.mark.parametrize("bad", ["yesterday", "2026-1-9", "2026/10/09", "20261009", "'; drop table", "2026-10-09-1"])
def test_malformed_day_is_400(client, bad):
    assert client.get("/positions/DEMO/report/daily", params={"day": bad}).status_code == 400


def test_run_route_stores_pushes_and_the_daily_route_returns_it(client, db):
    seed_trade(db, "AAA", mode="DEMO", opened_min=30, closed_min=1)
    r = client.post("/positions/DEMO/report/daily/run")
    assert r.status_code == 200
    body = r.json()
    assert body == {"mode": "DEMO", "day": ist_today_str(), "trades_today": 1, "stored": True}
    assert len(client.pushed) == 1
    got = client.get("/positions/DEMO/report/daily")
    assert got.status_code == 200 and got.json()["today"]["overall"]["trades"] == 1
    assert client.get("/positions/DEMO/report/daily", params={"day": ist_today_str()}).status_code == 200


def test_run_route_works_on_a_day_without_trades(client):
    r = client.post("/positions/DEMO/report/daily/run")
    assert r.status_code == 200 and r.json()["trades_today"] == 0
    assert len(client.pushed) == 1 and "No closed trades today." in client.pushed[0]


def test_run_route_is_500_when_the_report_cannot_be_stored(client, monkeypatch):
    monkeypatch.setattr(tr, "save_daily_snapshot", lambda *a, **k: False)
    assert client.post("/positions/DEMO/report/daily/run").status_code == 500
    assert client.pushed == []


def test_run_twice_overwrites_and_history_shows_one_line_per_day(client, db):
    seed_trade(db, "AAA", mode="DEMO", opened_min=30, closed_min=1)
    assert client.post("/positions/DEMO/report/daily/run").status_code == 200
    assert client.post("/positions/DEMO/report/daily/run").status_code == 200
    days = client.get("/positions/DEMO/report/history").json()
    assert days["mode"] == "DEMO" and [d["day"] for d in days["days"]] == [ist_today_str()]
    assert days["days"][0]["trades"] == 1


def test_history_route_limit_is_clamped_and_empty_is_fine(client):
    assert client.get("/positions/DEMO/report/history").json() == {"mode": "DEMO", "days": []}
    assert client.get("/positions/DEMO/report/history", params={"limit": 0}).status_code == 200
    assert client.get("/positions/DEMO/report/history", params={"limit": 99999}).status_code == 200


def test_bad_mode_is_400_and_real_needs_admin(client):
    for path in ("report/daily", "report/history"):
        assert client.get(f"/positions/NOPE/{path}").status_code == 400
        assert client.get(f"/positions/REAL/{path}").status_code in (401, 403)
    assert client.post("/positions/NOPE/report/daily/run").status_code == 400
    assert client.post("/positions/REAL/report/daily/run").status_code in (401, 403)


def test_new_routes_do_not_shadow_the_existing_ones(client):
    import main
    paths = {(r.path, m) for r in main.app.routes if hasattr(r, "methods") for m in r.methods}
    assert ("/positions/{mode}/{position_id}/close", "POST") in paths
    for p in ("/positions/{mode}/report", "/positions/{mode}/report/daily", "/positions/{mode}/report/history"):
        assert (p, "GET") in paths
    assert ("/positions/{mode}/report/daily/run", "POST") in paths


# ── group293: charges booked after the first report ─────────────────────────────────────────────────────────────────

def test_charges_booked_after_the_report_correct_the_snapshot_and_send_one_update(tick, monkeypatch):
    from resilience import local_cache
    monkeypatch.setattr(ap.config, "DAILY_REPORT_REFRESH_MINUTES", 15.0)
    monkeypatch.setattr(ap, "ist_time_at_or_after", lambda t, *a, **k: t.hour < 18)     # report time passed, cutoff not
    s = tick["factory"]()
    try:
        seed_trade(s, "AAA", mode="REAL", opened_min=30, closed_min=1, charges=None)     # ledger has not booked anything
    finally:
        s.close()
    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        first = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert tr.charges_pending(first) == 1 and first["today"]["overall"]["charges"] is None
    assert len(tick["pushed"]) == 1 and "charges still estimated" in tick["pushed"][0]

    asyncio.run(ap._schedule_tick_body("REAL"))                                          # too soon: nothing happens
    assert len(tick["pushed"]) == 1

    s = tick["factory"]()                                                                # the ledger books both orders
    try:
        for o in s.query(models.TradeOrder).filter_by(mode="REAL").all():
            s.add(models.TradeChargesLedger(order_id=o.id, mode="REAL", symbol="AAA", side=o.side,
                                            day=ist_today_str(), total_charges=7.0))
        s.commit()
        aged = dict(first, generated_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat())
        local_cache.save_snapshot(s, tr.snapshot_key("REAL", ist_today_str()), aged)
    finally:
        s.close()

    asyncio.run(ap._schedule_tick_body("REAL"))
    s = tick["factory"]()
    try:
        final = tr.load_daily_snapshot(s, "REAL", ist_today_str())
    finally:
        s.close()
    assert tr.charges_pending(final) == 0 and final["today"]["overall"]["charges"] == 14.0
    assert final["refresh_count"] == 1 and final["records_today"][0]["charges_source"] == "ledger"
    assert len(tick["pushed"]) == 2 and "(updated)" in tick["pushed"][1] and "still estimated" not in tick["pushed"][1]

    asyncio.run(ap._schedule_tick_body("REAL"))                                          # final: never rebuilt again
    assert len(tick["pushed"]) == 2
