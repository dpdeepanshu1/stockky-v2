"""Round 388 (E404): CORE no longer enters when its intraday part could not be scored (09:30:02 entries on daily + chain alone, 11 Nov)."""
from __future__ import annotations

from datetime import datetime

from fno import analytics, strategies as S
from fno.params import Params
from fno.provider import IST
from helpers import make_chain, make_daily, make_intraday

NOW = datetime(2026, 11, 10, 9, 30, 2, tzinfo=IST)
T0 = int(datetime(2026, 11, 10, 9, 15, tzinfo=IST).timestamp())          # the session's first 5-minute bar


def _ctx(intraday, p=None, now=NOW):
    p = p or Params().with_(min_score=20.0)
    ch = make_chain(spot=24500.0, expiry="2026-11-17", underlying="NIFTY", step=50.0, now=now.replace(tzinfo=None), lot=65, fetched_at=now.timestamp(), n_each=24)
    return S.build_ctx("NIFTY", make_daily(drift=0.004), intraday, analytics.chain_summary(ch, p.strikes_each_side), now, p)


def test_core_is_vetoed_without_a_scoreable_intraday_part():
    x = _ctx(make_intraday(n=4, step=6.0, t0=T0))               # 09:30:02: the 09:15-09:30 bars (the last one just forming): intraday_score says "too few intraday candles"
    b = S.core_bias(x)
    assert b["direction"] != "FLAT" and "too few intraday candles" in b["reasons"]       # the old rule would have entered
    assert S.candidates(x, [S.CORE], b) == []
    assert x.vetoes and x.vetoes[-1]["intraday"] == "missing" and x.vetoes[-1]["strategy"] == S.CORE


def test_core_with_no_intraday_candles_at_all_is_vetoed():
    x = _ctx([])
    b = S.core_bias(x)
    assert b["direction"] != "FLAT"                                  # the premise: it would have entered
    assert S.candidates(x, [S.CORE], b) == [] and x.vetoes[-1]["intraday"] == "missing"


def test_switch_off_restores_the_old_behaviour():
    x = _ctx(make_intraday(n=4, step=6.0, t0=T0), Params().with_(min_score=20.0, core_min_intraday_bars=0))
    b = S.core_bias(x)
    assert b["direction"] != "FLAT" and [c["strategy"] for c in S.candidates(x, [S.CORE], b)] == [S.CORE]


def test_core_with_enough_bars_is_unchanged():
    later = datetime(2026, 11, 10, 10, 0, 2, tzinfo=IST)               # 10:00: nine bars, six or more completed
    x = _ctx(make_intraday(n=8, step=6.0, t0=T0), now=later)
    b = S.core_bias(x)
    assert b["direction"] != "FLAT" and "too few intraday candles" not in b["reasons"]
    assert [c["strategy"] for c in S.candidates(x, [S.CORE], b)] == [S.CORE] and not x.vetoes


def test_backtest_evaluate_follows_the_same_rule_round389():
    """Round 389 (E405): SIM / rolling backtests call strategies.evaluate, not candidates: they must not enter where live no longer does."""
    x = _ctx(make_intraday(n=4, step=6.0, t0=T0))
    assert S.core_bias(x)["direction"] != "FLAT"
    ev = S.evaluate(S.CORE, x)
    assert ev["direction"] == "FLAT" and ev["strategy"] == S.CORE and "too few intraday candles" in ev["reasons"][0]
    x0 = _ctx(make_intraday(n=4, step=6.0, t0=T0), Params().with_(min_score=20.0, core_min_intraday_bars=0))
    assert S.evaluate(S.CORE, x0)["direction"] != "FLAT"                   # switch off = the old result
    later = datetime(2026, 11, 10, 10, 0, 2, tzinfo=IST)
    assert S.evaluate(S.CORE, _ctx(make_intraday(n=8, step=6.0, t0=T0), now=later))["direction"] != "FLAT"
