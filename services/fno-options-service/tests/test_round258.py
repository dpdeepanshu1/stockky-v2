"""Round 258: a rolling backtest on too few strikes cannot look good. A held strike with no candle is not managed (no stop, no target); the result carries data_quality and the verdict drops to NOT_PROVEN."""
from __future__ import annotations

from backtest import report, rolling_sim
from fno.params import Params
from test_backtests import build_world
from test_round246 import book_from, make_files
from test_round260 import vix_on

P = Params(min_score=30, max_trades_per_day=2)


def test_enough_strikes_is_ok_and_reported():
    daily, intr = build_world()
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), vix_by_day=vix_on(daily), p=P)       # round 260: ok also needs a VIX
    dq = r["data_quality"]
    assert dq["ok"] is True and dq["unmanaged_pct"] <= rolling_sim.MAX_UNMANAGED_PCT and dq["held_bars"] > 0 and "ATM+1" in dq["labels"] and "ATM-2" in dq["labels"]
    assert "data quality:" in report.render_text(r) and "OK" in report.render_text(r).split("data quality:")[1].splitlines()[0]


def test_a_book_with_holes_in_the_held_strike_is_not_proven():
    daily, intr = build_world()
    docs = [d for d in make_files(intr) if d["strike"] == "ATM"]
    for d in docs:
        d["rows"] = [r for i, r in enumerate(d["rows"]) if i % 3 == 0]          # two bars in three have no candle: the held strike is mostly unpriced
    r = rolling_sim.run("NIFTY", daily, intr, book_from(docs), p=P)
    dq = r["data_quality"]
    assert dq["labels"] == ["ATM"] and dq["held_bars"] > 0 and dq["unmanaged_pct"] > rolling_sim.MAX_UNMANAGED_PCT and dq["ok"] is False
    assert r["verdict"]["status"] == "NOT_PROVEN" and "no candle" in r["verdict"]["reasons"][0]
    assert "NOT OK" in report.render_text(r)


def test_the_gate_itself(monkeypatch):
    daily, intr = build_world()
    monkeypatch.setattr(rolling_sim, "MAX_UNMANAGED_PCT", -1.0)       # any held bar over the limit
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), p=P)
    assert r["data_quality"]["held_bars"] > 0
    assert r["data_quality"]["ok"] is False and r["verdict"]["status"] == "NOT_PROVEN" and "stops / targets were not applied" in r["verdict"]["reasons"][0]
