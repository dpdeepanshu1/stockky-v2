"""Round 262, slice 3: the readiness checklist (fno/readiness.py), the REAL lock that waits for it (execution.real_allowed) and the two routes. No network, no money."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace as NS

import pytest

import config
from fno import execution, readiness
from fno.paper import Position
from fno.store import Store, day_of

NOW = 1_794_000_000.0          # fixed epoch, mid November 2026


def good_input(**over):
    inp = {"now": NOW, "demo": {"days": 25, "trades": 70, "net": 5400.0},
           "backtests": {"CORE": {"NIFTY": {"trades": 45, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}}},
           "reconcile": {"ts": NOW - 30, "ok": True}, "board": {"capital": 120000.0}, "creds": True,
           "snapshots": {"days": 12, "today": 300}, "manual": {k: {"ok": True, "ts": NOW - 86400} for k in readiness.MANUAL}}
    inp.update(over)
    return inp


def by_key(r):
    return {i["key"]: i for i in r["items"]}


# ── evaluate (pure) ──
def test_everything_in_place_is_all_green():
    r = readiness.evaluate(good_input())
    assert r["all_green"] and r["blockers"] == [] and r["counts"]["red"] == 0 and r["counts"]["hard_open"] == 0


def test_an_empty_service_is_never_green_and_never_says_safe():
    r = readiness.evaluate({"now": NOW})
    k = by_key(r)
    assert not r["all_green"] and r["counts"]["green"] == 0
    assert k["demo_net"]["ok"] is None                       # no trade yet = unknown, not a pass and not a fail
    assert k["demo_days"]["ok"] is False and k["capital_board"]["ok"] is False and k["reconcile"]["ok"] is None
    assert all(i["ok"] is not True for i in r["items"] if i["hard"])


def test_demo_thresholds_and_negative_net_block():
    k = by_key(readiness.evaluate(good_input(demo={"days": 19, "trades": 59, "net": -10.0})))
    assert k["demo_days"]["ok"] is False and k["demo_trades"]["ok"] is False and k["demo_net"]["ok"] is False
    k = by_key(readiness.evaluate(good_input(demo={"days": 20, "trades": 60, "net": 1.0})))
    assert k["demo_days"]["ok"] and k["demo_trades"]["ok"] and k["demo_net"]["ok"]


@pytest.mark.parametrize("row,ok", [
    (None, None),
    ({"trades": 45, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}, True),
    ({"trades": 45, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": False}, False),
    ({"trades": 45, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY"}, False),                  # a result with no data_quality block is not proof
    ({"trades": 29, "net": 9000, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}, False),
    ({"trades": 45, "net": 0, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}, False),
    ({"trades": 45, "net": 9000, "verdict": "NOT_PROVEN", "dq_ok": True}, False),
])
def test_backtest_check_follows_the_phase_one_exit_rule(row, ok):
    assert readiness.backtest_check(row)[0] is ok


def test_a_strategy_needs_every_traded_underlying_to_pass():
    bt = {"CORE": {"NIFTY": {"trades": 45, "net": 1, "verdict": "PASSED_THIS_TEST_ONLY", "dq_ok": True}, "SENSEX": None}}
    k = by_key(readiness.evaluate(good_input(backtests=bt)))
    assert k["backtest_core"]["ok"] is None and "SENSEX: no rolling backtest" in k["backtest_core"]["detail"]
    bt["CORE"]["SENSEX"] = {"trades": 5, "net": 1, "verdict": "NOT_PROVEN", "dq_ok": True}
    assert by_key(readiness.evaluate(good_input(backtests=bt)))["backtest_core"]["ok"] is False


def test_reconcile_states():
    for rec, ok in (({"ts": NOW - 30, "ok": True}, True), ({"ts": NOW - 30, "ok": False, "differences": [{"security_id": "1"}]}, False),
                    ({"ts": NOW - 5000, "ok": True}, None), ({"ts": None}, None), ({"ts": NOW, "ok": None, "note": "broker positions unavailable"}, None)):
        assert by_key(readiness.evaluate(good_input(reconcile=rec)))["reconcile"]["ok"] is ok


def test_manual_checks_are_hard_and_only_green_when_confirmed():
    m = {k: {"ok": True, "ts": NOW} for k in readiness.MANUAL}
    m["dhan_static_ip"] = {"ok": False, "ts": NOW}           # withdrawn
    r = readiness.evaluate(good_input(manual=m))
    k = by_key(r)
    assert k["dhan_static_ip"]["ok"] is None and k["dhan_static_ip"]["hard"] and k["dhan_static_ip"]["manual"]
    assert not r["all_green"] and any("static IP" in b for b in r["blockers"])


def test_credentials_and_snapshots_today_are_shown_but_do_not_block():
    r = readiness.evaluate(good_input(creds=False, snapshots={"days": 12, "today": 0}))
    k = by_key(r)
    assert k["dhan_credentials"]["ok"] is None and not k["dhan_credentials"]["hard"]
    assert k["snapshots_today"]["ok"] is None and not k["snapshots_today"]["hard"]
    assert r["all_green"]                                     # the hard items are still all green


# ── reading the service's own data ──
def closed_pos(day_offset, mode="DEMO", net=100.0, parent=None):
    t = NOW - day_offset * 86400
    return Position(underlying="NIFTY", expiry="2026-11-17", strike=24500.0, opt_type="CE", lot_size=65, lots=1, entry_ts=t, entry_premium=100.0, sl_price=75.0, target_price=150.0,
                    mode=mode, status="CLOSED", exit_ts=t + 600, exit_premium=110.0, exit_reason="TARGET", gross_pnl=net + 20, charges=20.0, net_pnl=net, parent_id=parent)


def test_demo_evidence_counts_days_and_trades_not_partial_child_rows():
    s = Store(":memory:")
    for d in (0, 0, 1, 3):
        s.insert_position(closed_pos(d))
    s.insert_position(closed_pos(3, net=-30.0, parent=1))      # a partial-exit child: its net counts, it is not a new trade
    s.insert_position(closed_pos(5, mode="REAL"))              # REAL rows are not DEMO evidence
    e = readiness.demo_evidence(s)
    assert e == {"days": 3, "trades": 4, "net": 370.0}
    s.close()


def write_bt(root, name, strategy="CORE", und="NIFTY", kind="ROLLING", trades=40, net=500, verdict="PASSED_THIS_TEST_ONLY", dq=True, mtime=None):
    d = os.path.join(root, "backtests")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    json.dump({"kind": kind, "strategy": strategy, "underlying": und, "metrics": {"overall": {"trades": trades, "net": net}}, "verdict": {"status": verdict}, "data_quality": {"ok": dq}}, open(p, "w"))
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def test_latest_rolling_takes_the_newest_per_strategy_and_underlying_and_skips_junk(tmp_path):
    root = str(tmp_path)
    write_bt(root, "fno_rolling_old.json", net=-5, verdict="NOT_PROVEN", mtime=1_000_000)
    write_bt(root, "fno_rolling_new.json", net=700, mtime=2_000_000)
    write_bt(root, "fno_rolling_sensex.json", und="SENSEX", mtime=1_500_000)
    write_bt(root, "fno_sim_x.json", kind="SIM")                # not a rolling result
    open(os.path.join(root, "backtests", "fno_rolling_bad.json"), "w").write("{not json")
    got = readiness.latest_rolling(root)
    assert set(got) == {("CORE", "NIFTY"), ("CORE", "SENSEX")}
    assert got[("CORE", "NIFTY")]["net"] == 700 and got[("CORE", "NIFTY")]["dq_ok"] is True
    assert readiness.latest_rolling(os.path.join(root, "nowhere")) == {}


def test_required_backtests_skip_expiry_day_strategies_unless_real_expiry_is_on(tmp_path):
    root = str(tmp_path)
    write_bt(root, "fno_rolling_a.json")
    cfg = NS(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True, FNO_STRAT_TREND_RIDER=True, FNO_STRAT_EXPIRY_ALPHA=True, FNO_EXPIRY_REAL=False)
    req = readiness.required_backtests(cfg, root)
    assert set(req) == {"CORE", "TREND_RIDER"} and req["CORE"]["NIFTY"]["trades"] == 40 and req["TREND_RIDER"]["NIFTY"] is None
    cfg.FNO_EXPIRY_REAL = True
    assert "EXPIRY_ALPHA" in readiness.required_backtests(cfg, root)


def test_collect_reads_the_store_and_never_raises(tmp_path):
    s = Store(":memory:")
    for d in range(21):
        for _ in range(3):
            s.insert_position(closed_pos(d))
    for d in range(11):
        s.add_snapshot(NOW - d * 86400, "NIFTY", "2026-11-17", "x")
    s.add_snapshot(NOW, "NIFTY", "2026-11-17", "x")
    for k in readiness.MANUAL:
        readiness.set_manual(s, k, True, NOW)
    write_bt(str(tmp_path), "fno_rolling_a.json")
    cfg = NS(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True, DHAN_CLIENT_ID="1", DHAN_ACCESS_TOKEN="t")
    r = readiness.collect(s, cfg, str(tmp_path), {"ts": NOW - 10, "ok": True}, {"real_sizing_capital": 50000.0}, NOW)
    assert r["all_green"], r["blockers"]
    broken = readiness.collect(object(), NS(), "/definitely/not/here", None, None, NOW)      # nothing readable: unknown / red, no exception
    assert not broken["all_green"]
    s.close()


def test_set_manual_rejects_unknown_keys_and_can_withdraw():
    s = Store(":memory:")
    with pytest.raises(KeyError):
        readiness.set_manual(s, "made_up", True)
    readiness.set_manual(s, "dhan_static_ip", True, NOW)
    readiness.set_manual(s, "dhan_static_ip", False, NOW + 1)
    assert s.kv_get(readiness.MANUAL_KV)["dhan_static_ip"]["ok"] is False
    s.close()


# ── the REAL lock ──
def open_cfg(**kw):
    base = dict(FNO_ENGINE_ENABLED=True, FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1", DHAN_ACCESS_TOKEN="t", FNO_REAL_REQUIRES_READINESS=True, FNO_READINESS_BLOCKERS=[])
    base.update(kw)
    return NS(**base)


def test_real_lock_waits_for_the_list_and_fails_closed():
    assert execution.real_allowed(open_cfg(), "d", "d") == (True, [])
    ok, closed = execution.real_allowed(open_cfg(FNO_READINESS_BLOCKERS=["DEMO net after charges is positive (net -5)"]), "d", "d")
    assert not ok and "readiness list not green" in closed[0] and "DEMO net" in closed[0]
    ok, closed = execution.real_allowed(open_cfg(FNO_READINESS_BLOCKERS=None), "d", "d")
    assert not ok and "not computed yet" in closed[0]


def test_switching_the_wait_off_or_an_old_config_without_the_attribute_leaves_the_old_behaviour():
    assert execution.real_allowed(open_cfg(FNO_REAL_REQUIRES_READINESS=False, FNO_READINESS_BLOCKERS=["x"]), "d", "d")[0] is True
    old = NS(FNO_ENGINE_ENABLED=True, FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1", DHAN_ACCESS_TOKEN="t")
    assert execution.real_allowed(old, "d", "d")[0] is True


def test_the_shipped_config_waits_for_the_list():
    assert config.FNO_REAL_REQUIRES_READINESS is True            # ships ON; the Calibration switch or the env file can turn it off
    assert execution.real_allowed(NS(**{k: getattr(config, k) for k in dir(config) if k.isupper()}, ), "d", "d")[0] is False


def test_publish_sets_the_blockers_throttles_and_fails_closed(tmp_path, monkeypatch):
    s = Store(":memory:")
    cfg = NS(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True, CAPITAL_ALLOC_MODE="off")
    readiness._last_pub["t"] = 0.0
    assert readiness.publish(cfg, s, str(tmp_path), {"ts": None}, None, now=NOW) is True
    assert isinstance(cfg.FNO_READINESS_BLOCKERS, list) and cfg.FNO_READINESS_BLOCKERS           # an empty service has open hard items
    assert readiness.publish(cfg, s, str(tmp_path), {"ts": None}, None, now=NOW + 5) is False    # throttled
    monkeypatch.setattr(readiness, "collect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert readiness.publish(cfg, s, str(tmp_path), {"ts": None}, None, now=NOW + 100) is True
    assert "could not be computed" in cfg.FNO_READINESS_BLOCKERS[0]
    s.close()


# ── routes ──
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    monkeypatch.setattr(config, "FNO_READINESS_BLOCKERS", None, raising=False)
    yield TestClient(main.app), main
    main.app.dependency_overrides.clear()


def test_readiness_route_lists_items_and_refreshes_the_blockers(client):
    c, main = client
    d = c.get("/fno/readiness").json()
    assert d["real_waits_for_it"] is True and not d["all_green"] and d["items"] and d["thresholds"]["demo_days"] == readiness.MIN_DEMO_DAYS
    assert config.FNO_READINESS_BLOCKERS == d["blockers"] and d["locks"]["real_orders_possible"] is False


def test_confirm_route_records_the_check_logs_it_and_rejects_unknown_keys(client):
    c, main = client
    r = c.post("/fno/readiness/confirm", json={"key": "dhan_static_ip", "ok": True})
    assert r.status_code == 200 and {i["key"]: i for i in r.json()["items"]}["dhan_static_ip"]["ok"] is True
    assert any(e.get("action") == "readiness_confirm" and e.get("key") == "dhan_static_ip" for e in main.store.events("manual"))
    assert c.post("/fno/readiness/confirm", json={"key": "nope", "ok": True}).status_code == 400
    r = c.post("/fno/readiness/confirm", json={"key": "dhan_static_ip", "ok": False})
    assert {i["key"]: i for i in r.json()["items"]}["dhan_static_ip"]["ok"] is None
