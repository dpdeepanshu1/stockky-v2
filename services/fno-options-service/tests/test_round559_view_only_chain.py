"""Round 559 (P7): BANKNIFTY in the chain viewer ONLY. The Chain tab can read its expiries and chain; the engine, the recorder, the backtests and every order path still know
NIFTY and SENSEX only, and no bias (signal) is computed for it. The route tests need fastapi and run on the VM (`bash scripts/docker_test.sh fno-options-service`);
the rest runs anywhere.
Run from services/fno-options-service:   python3 -m pytest tests/test_round559_view_only_chain.py -q"""
from __future__ import annotations

import os
import re
import types

import pytest

import config
from fno import engine as eng, provider as prov
from fno.store import Store
from helpers import make_chain

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


def test_the_engine_never_lists_banknifty_whatever_the_switch_says():
    cases = {"BOTH": ["NIFTY", "SENSEX"], "NIFTY": ["NIFTY"], "SENSEX": ["SENSEX"], "BANKNIFTY": [], "banknifty": [], "FINNIFTY": [], "": []}
    for value, want in cases.items():
        me = types.SimpleNamespace(cfg=types.SimpleNamespace(FNO_UNDERLYINGS=value))
        assert eng.Engine.underlyings(me) == want, value


def test_view_only_list_is_banknifty_alone_and_is_not_an_engine_or_index_candle_underlying():
    assert prov.VIEW_ONLY_UNDERLYINGS == ("BANKNIFTY",)
    assert set(prov.INDEX_SYMBOL) == {"NIFTY", "SENSEX"}, "no candles are read for a view-only index: a stray call would fail loudly"
    assert "BANKNIFTY" not in config.FNO_UNDERLYINGS.upper()


def test_the_switch_catalog_still_offers_nifty_sensex_both_only():
    from control.catalog import CATALOG
    flag = next(f for f in CATALOG if f.key == "FNO_UNDERLYINGS")
    assert tuple(flag.choices) == ("nifty", "sensex", "both")


def test_backtests_and_the_sim_still_refuse_banknifty():
    from backtest import report
    assert report.request_problem("sim", ["BANKNIFTY"], "CORE", None, ["NIFTY", "SENSEX"]) == "underlyings must be NIFTY and/or SENSEX"


def test_chain_route_source_skips_the_bias_for_a_view_only_index_and_only_there():
    s = _src("main.py")
    i = s.index('@app.get("/fno/chain")')
    block = s[i:s.index('@app.get("/fno/strategies")', i)]
    assert "VIEW_ONLY_UNDERLYINGS" in block and "view_only" in block
    assert re.search(r"bias = None if view_only else signals\.index_bias\(", block)
    assert block.count("signals.index_bias(") == 1
    for rel in ("fno/engine.py", "fno/runtime.py", "fno/recorder.py", "fno/signals.py", "fno/execution.py", "backtest/replay.py", "backtest/sim.py", "backtest/report.py"):
        assert "VIEW_ONLY" not in _src(rel) and "BANKNIFTY" not in _src(rel), rel


# ── the routes (fastapi needed) ───────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    yield TestClient(main.app), main
    main.app.dependency_overrides.clear()


def _wire(main, monkeypatch, seen):
    def chain(u, e):
        seen.append(("chain", u, e))
        return make_chain(spot=55040.0, expiry="2026-11-24", underlying=u, step=100.0, lot=30) if u == "BANKNIFTY" else make_chain(underlying=u)
    monkeypatch.setattr(main.provider, "chain", chain)
    monkeypatch.setattr(main.provider, "expiries", lambda u: ["2026-11-24", "2026-12-29"] if u == "BANKNIFTY" else ["2026-11-17"])
    monkeypatch.setattr(main.provider, "daily", lambda u: seen.append(("daily", u)) or [])
    monkeypatch.setattr(main.provider, "intraday", lambda u: seen.append(("intraday", u)) or [])
    monkeypatch.setattr(main.provider, "vix", lambda: 15.0)
    monkeypatch.setattr(main.engine, "pick_expiry", lambda u, *a, **k: "2026-11-24" if u == "BANKNIFTY" else "2026-11-17")


def test_banknifty_chain_route_answers_with_no_bias_and_reads_no_candles(client, monkeypatch):
    c, main = client
    seen = []
    _wire(main, monkeypatch, seen)
    r = c.get("/fno/chain?underlying=banknifty")
    assert r.status_code == 200
    d = r.json()
    assert d["view_only"] is True and d["bias"] is None and d["chain"]["underlying"] == "BANKNIFTY" and d["expiries"] == ["2026-11-24", "2026-12-29"]
    assert {"summary", "age_s", "vix_rank"} <= set(d)
    assert [x for x in seen if x[0] in ("daily", "intraday")] == [], "no index candles are read for a view-only index"
    assert ("chain", "BANKNIFTY", "2026-11-24") in seen


def test_nifty_and_sensex_chain_routes_are_unchanged(client, monkeypatch):
    c, main = client
    seen = []
    _wire(main, monkeypatch, seen)
    for u in ("NIFTY", "SENSEX"):
        d = c.get(f"/fno/chain?underlying={u}").json()
        assert d["view_only"] is False and isinstance(d["bias"], dict) and "direction" in d["bias"]
    assert ("daily", "NIFTY") in seen and ("intraday", "SENSEX") in seen


def test_other_indices_are_still_refused_with_400(client, monkeypatch):
    c, main = client
    _wire(main, monkeypatch, [])
    for u in ("FINNIFTY", "MIDCPNIFTY", "RELIANCE"):
        assert c.get(f"/fno/chain?underlying={u}").status_code == 400
        assert c.get(f"/fno/expiries?underlying={u}").status_code == 400


def test_expiries_route_marks_a_view_only_index(client, monkeypatch):
    c, main = client
    _wire(main, monkeypatch, [])
    d = c.get("/fno/expiries?underlying=BANKNIFTY").json()
    assert d == {"underlying": "BANKNIFTY", "expiries": ["2026-11-24", "2026-12-29"], "view_only": True}
    assert c.get("/fno/expiries?underlying=NIFTY").json()["view_only"] is False
