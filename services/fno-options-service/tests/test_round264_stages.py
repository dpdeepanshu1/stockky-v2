"""Round 264, slice 2: GET /fno/pipeline as eight ordered stages (fno/stages.py). Pure `build` on made-up journal events, plus the route. No network, no money."""
from __future__ import annotations

import pytest

import config
from fno import stages
from fno.store import Store, day_of

NOW = 1_794_000_000.0          # fixed epoch, mid November 2026


def ev(kind, ago=60.0, **kw):
    return {"id": 1, "ts": NOW - ago, "kind": kind, "underlying": kw.pop("underlying", "NIFTY"), **kw}


def tick(ok=True, age=20.0, decisions=None, error=None):
    return {"ts": NOW - age, "ok": ok, "error": error, "summary": {"decisions": decisions if decisions is not None else {"NIFTY": {"action": "none", "direction": "UP", "score": 62}}}}


def inp(**over):
    base = {"now": NOW, "enabled": True, "market_open": True, "md_ok": True, "vix_ok": True, "expiries_ok": True, "md_detail": "", "last_tick": tick(), "events": {},
            "open_positions": 0, "broker": False, "reconcile": {"ts": None}}
    base.update(over)
    return base


def run(**over):
    return {s["key"]: s for s in stages.build(inp(**over), day_of)}


def test_eight_stages_in_the_planned_order():
    out = stages.build(inp(), day_of)
    assert [s["key"] for s in out] == ["market_data", "chains", "signal", "gates", "sizing", "order", "exits", "reconcile"]
    assert all(set(s) == {"key", "label", "status", "detail", "age_s", "last_decision", "count_today"} for s in out)


def test_a_quiet_healthy_day_is_ok_where_it_ran_and_idle_where_nothing_happened():
    k = run()
    assert k["market_data"]["status"] == "OK" and k["chains"]["status"] == "OK" and k["signal"]["status"] == "OK"
    assert k["order"]["status"] == "idle" and k["exits"]["status"] == "idle" and k["reconcile"]["status"] == "idle"
    assert k["chains"]["age_s"] == 20.0


def test_market_data_problem_names_what_is_missing_and_works_with_the_engine_off():
    k = run(md_ok=False, vix_ok=False, md_detail="timeout", enabled=False)
    assert k["market_data"]["status"] == "PROBLEM" and "India VIX" in k["market_data"]["detail"] and "timeout" in k["market_data"]["detail"]
    assert k["chains"]["status"] == "idle" and k["chains"]["detail"] == "engine is OFF"


@pytest.mark.parametrize("over,status,word", [
    ({"market_open": False}, "idle", "closed"),
    ({"last_tick": {"ts": None, "ok": None, "summary": {}}}, "idle", "no engine cycle"),
    ({"last_tick": tick(ok=False, error="KeyError: x")}, "PROBLEM", "KeyError"),
    ({"last_tick": tick(age=300)}, "PROBLEM", "300 s"),
    ({"last_tick": tick(decisions={"NIFTY": {"action": "wait", "why": ["no chain"]}})}, "PROBLEM", "no chain"),
    ({"last_tick": tick(decisions={})}, "idle", "no index"),
])
def test_chains_stage_states(over, status, word):
    c = run(**over)["chains"]
    assert c["status"] == status and word in c["detail"]


def test_signal_waits_when_the_chain_is_bad_and_never_looks_ok_without_a_chain():
    k = run(last_tick=tick(ok=False, error="boom"))
    assert k["signal"]["status"] == "idle" and "no chain" in k["signal"]["detail"]


def test_gate_blocks_are_the_gates_working_not_a_problem_and_carry_the_reason():
    e = ev("skip", stage="gates", strategy="CORE", why=["India VIX 28 above 25"], underlying="NIFTY")
    g = run(events={"skip": [e]})["gates"]
    assert g["status"] == "OK" and g["count_today"] == 1 and "India VIX 28" in g["detail"]
    assert g["last_decision"]["kind"] == "skip" and "stopped at gates" in g["last_decision"]["text"] and g["last_decision"]["data"]["why"] == ["India VIX 28 above 25"]


def test_no_capital_is_a_sizing_problem_until_an_entry_comes_after_it():
    cap = ev("skip", ago=300, stage="capital", strategy="CORE", reasons=["no fresh capital board"])
    assert run(events={"skip": [cap]})["sizing"]["status"] == "PROBLEM"
    later = ev("entry", ago=100, label="NIFTY 24500 CE", lots=1, fill=100, mode="DEMO")
    k = run(events={"skip": [cap], "entry": [later]})
    assert k["sizing"]["status"] == "OK" and k["order"]["status"] == "OK" and k["order"]["count_today"] == 1


def test_other_sizing_stops_are_not_a_fault():
    s = run(events={"skip": [ev("skip", stage="sizing", strategy="CORE", reasons=["one lot exceeds the risk budget"])]})["sizing"]
    assert s["status"] == "OK" and "sizing" in s["detail"]


def test_a_failed_buy_is_an_order_problem_but_a_failed_stop_is_an_exit_problem_only():
    buy = ev("order_failed", side="BUY", message="rejected")
    k = run(events={"order_failed": [buy]})
    assert k["order"]["status"] == "PROBLEM" and "rejected" in k["order"]["detail"] and k["exits"]["status"] == "idle"
    sl = ev("order_failed", side="SL", message="no broker-side stop", position=3)
    k = run(events={"order_failed": [sl]}, open_positions=1)
    assert k["order"]["status"] == "idle" and k["exits"]["status"] == "PROBLEM"


def test_real_blocked_buy_is_an_order_problem_and_real_blocked_sell_an_exit_problem():
    k = run(events={"real_blocked": [ev("real_blocked", would="BUY", locks=["ARM"])]})
    assert k["order"]["status"] == "PROBLEM" and "nothing was sent" in k["order"]["detail"]
    k = run(events={"real_blocked": [ev("real_blocked", would="SELL", locks=["ARM"])]}, open_positions=1)
    assert k["exits"]["status"] == "PROBLEM" and "closed locks" in k["exits"]["detail"] and k["order"]["status"] == "idle"


def test_an_exit_after_the_failure_clears_the_exit_problem():
    fail = ev("order_failed", ago=300, side="SELL", message="x")
    done = ev("exit", ago=100, label="NIFTY 24500 CE", reason="TARGET", net=900)
    k = run(events={"order_failed": [fail], "exit": [done]}, open_positions=1)
    assert k["exits"]["status"] == "OK" and k["exits"]["last_decision"]["text"].endswith("net 900")


def test_a_position_without_a_premium_is_an_exit_problem_only_while_it_is_open():
    st = ev("stale", position=1, msg="no premium for an open position")
    assert run(events={"stale": [st]}, open_positions=1)["exits"]["status"] == "PROBLEM"
    assert run(events={"stale": [st]}, open_positions=0)["exits"]["status"] == "idle"


def test_yesterdays_events_do_not_count_today():
    old = ev("order_failed", ago=3 * 86400, side="BUY", message="old")
    assert run(events={"order_failed": [old]})["order"]["status"] == "idle"


@pytest.mark.parametrize("rec,broker,status,word", [
    ({"ts": None}, False, "idle", "no Dhan credentials"),
    ({"ts": None}, True, "idle", "not checked"),
    ({"ts": NOW - 30, "ok": None, "note": "broker positions unavailable"}, True, "idle", "unavailable"),
    ({"ts": NOW - 30, "ok": False, "differences": [{"a": 1}, {"b": 2}]}, True, "PROBLEM", "2 difference"),
    ({"ts": NOW - 30, "ok": True}, True, "OK", "match"),
])
def test_reconcile_stage(rec, broker, status, word):
    r = run(reconcile=rec, broker=broker)["reconcile"]
    assert r["status"] == status and word in r["detail"]


def test_describe_each_kind_and_empty():
    assert stages.describe(None) is None
    for kind, extra in [("veto", {"direction": "UP", "why": ["oi"]}), ("signal", {"direction": "FLAT", "score": 3}), ("entry", {"label": "x", "lots": 1, "fill": 1, "mode": "DEMO"}),
                        ("exit", {"label": "x", "reason": "SL", "net": -1}), ("real_blocked", {"would": "BUY", "locks": ["ARM"]}), ("order_failed", {"side": "BUY", "message": "m"}),
                        ("stale", {}), ("reconcile", {"msg": "m"})]:
        d = stages.describe(ev(kind, **extra))
        assert d["kind"] == kind and d["text"] and "id" not in d["data"] and "ts" not in d["data"]


def test_build_never_raises_on_junk_events():
    out = stages.build(inp(events={"skip": [{"kind": "skip", "ts": "bad"}, {"kind": "skip"}], "entry": [None]}), day_of)
    assert len(out) == 8


def test_summary_counts():
    s = stages.summary(stages.build(inp(md_ok=False), day_of))
    assert s["problems"] == ["Market data"] and s["ok"] + s["idle"] + len(s["problems"]) == 8


# ── the route ──
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    monkeypatch.setattr(main.provider, "_get", lambda *a, **k: None)
    yield TestClient(main.app), main
    main.app.dependency_overrides.clear()


def test_pipeline_route_keeps_the_flat_steps_and_adds_the_stages(client):
    c, main = client
    d = c.get("/fno/pipeline").json()
    assert d["steps"] and [s["key"] for s in d["stages"]][0] == "market_data" and len(d["stages"]) == 8
    assert d["stages"][0]["status"] == "PROBLEM" and "Market data" in d["stage_summary"]["problems"] and "locks" in d


def test_pipeline_route_shows_todays_journal_in_the_stages(client):
    import time
    c, main = client
    main.store.add_event("skip", {"stage": "gates", "strategy": "CORE", "why": ["VIX too high"]}, underlying="NIFTY", ts=time.time() - 5)
    g = {s["key"]: s for s in c.get("/fno/pipeline").json()["stages"]}["gates"]
    assert g["status"] == "OK" and g["count_today"] == 1 and "VIX too high" in g["last_decision"]["text"]


def test_pipeline_route_survives_a_failing_stage_build(client, monkeypatch):
    c, main = client
    monkeypatch.setattr(main.stages, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = c.get("/fno/pipeline").json()
    assert d["steps"] and d["stages"] == [] and d["stage_summary"] is None
