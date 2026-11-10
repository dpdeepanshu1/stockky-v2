"""Round 278: a strategy rule that RAISES is recorded (it used to look exactly like "no signal"), the other strategies still run, and the cycle goes on."""
from __future__ import annotations

import pytest

from fno import strategies as S
from test_engine_paper import NOW, engine, make_cfg
from test_round236 import FLAT, SURGE, SurgeProvider, ctx, db  # noqa: F401  (db is a fixture)


def _boom(x):
    raise ZeroDivisionError("division by zero")


def test_a_raising_rule_is_recorded_and_the_others_still_run(monkeypatch):
    monkeypatch.setitem(S.STRATEGIES["TREND_RIDER"], "fn", _boom)
    x = ctx(FLAT(), SURGE, expiry_day=False)
    out = S.candidates(x, ["TREND_RIDER", "MOMENTUM_SURGE"])
    assert [c["strategy"] for c in out] == ["MOMENTUM_SURGE"]                 # the healthy rule still fired
    assert x.errors == [{"strategy": "TREND_RIDER", "error": "ZeroDivisionError: division by zero"}]


def test_a_healthy_run_records_no_error():
    x = ctx(FLAT(), SURGE, expiry_day=False)
    S.candidates(x, ["MOMENTUM_SURGE", "TREND_RIDER"])
    assert x.errors == []


def test_evaluate_tells_a_raise_from_a_quiet_rule(monkeypatch):
    monkeypatch.setitem(S.STRATEGIES["TREND_RIDER"], "fn", _boom)
    bad = S.evaluate("TREND_RIDER", ctx(FLAT(), SURGE, expiry_day=False))
    assert bad["direction"] == S.FLAT and bad["error"].startswith("ZeroDivisionError")
    quiet = S.evaluate("BEAR_PIVOT", ctx(FLAT(), SURGE, expiry_day=False))
    assert quiet["direction"] == S.FLAT and "error" not in quiet


def test_error_text_is_capped():
    def long(x):
        raise ValueError("x" * 5000)
    S.STRATEGIES["TREND_RIDER"]["fn"], keep = long, S.STRATEGIES["TREND_RIDER"]["fn"]
    try:
        x = ctx(FLAT(), SURGE, expiry_day=False)
        S.candidates(x, ["TREND_RIDER"])
        assert len(x.errors[0]["error"]) < 200
    finally:
        S.STRATEGIES["TREND_RIDER"]["fn"] = keep


def test_the_engine_writes_a_strategy_error_event_and_keeps_trading(db, monkeypatch):
    monkeypatch.setitem(S.STRATEGIES["TREND_RIDER"], "fn", _boom)
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False, FNO_STRAT_TREND_RIDER=True, FNO_STRAT_MOMENTUM_SURGE=True)
    out = engine(db, SurgeProvider(), cfg).tick(NOW)
    ev = [e for e in db.events() if e["kind"] == "strategy_error"]
    assert ev and ev[0]["strategy"] == "TREND_RIDER" and "ZeroDivisionError" in ev[0]["error"] and "no signal" in ev[0]["msg"]
    assert out["entries"]                                                     # MOMENTUM_SURGE still traded in the same cycle


def test_the_signals_route_lists_strategy_errors():
    import main
    src = open(main.__file__).read()
    assert '"strategy_error"' in src.split("def signals_log")[1].split("@app.get")[0]

