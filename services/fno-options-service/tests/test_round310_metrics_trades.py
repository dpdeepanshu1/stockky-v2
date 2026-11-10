"""Round 310: backtest/metrics counts TRADES (a partial exit is part of its parent's trade) and ignores NOT_FILLED entries, like Store.day_summary and fno/capital.stats."""
from __future__ import annotations

from backtest import metrics
from fno.paper import CLOSED, Position


def _p(net, ts, pid=None, parent=None, reason="TARGET", gross=None, entry=None):
    p = Position(underlying="NIFTY", expiry="2026-11-12", strike=24000, opt_type="CE", lot_size=65, lots=1, entry_ts=(ts - 60) if entry is None else entry, entry_premium=100.0, sl_price=75.0, target_price=150.0)
    p.id, p.parent_id, p.status, p.exit_ts, p.exit_reason, p.net_pnl = pid, parent, CLOSED, ts, reason, net
    p.gross_pnl, p.charges = (net + 10.0 if gross is None else gross), 10.0
    return p


def test_a_partial_exit_and_its_parent_are_one_trade_judged_on_the_sum():
    ps = [_p(-300, 1_800_000_100, pid=2, parent=1, reason="PARTIAL", entry=1_800_000_000), _p(900, 1_800_000_200, pid=1, entry=1_800_000_000)]      # child lost, parent won: the trade won
    o = metrics.summarize(ps)["overall"]
    assert o["trades"] == 1 and o["wins"] == 1 and o["net"] == 600 and o["win_rate_pct"] == 100.0


def test_a_not_filled_entry_is_not_a_trade_or_a_loss():
    ps = [_p(0.0, 1_800_000_100, pid=1, reason="NOT_FILLED", gross=0.0), _p(500, 1_800_000_200, pid=2)]
    o = metrics.summarize(ps)["overall"]
    assert o["trades"] == 1 and o["wins"] == 1 and o["win_rate_pct"] == 100.0 and o["longest_losing_streak"] == 0


def test_simulated_trades_without_ids_stay_separate():
    ps = [_p(100, 1_800_000_000 + i * 10) for i in range(5)]
    o = metrics.summarize(ps)["overall"]
    assert o["trades"] == 5 and o["wins"] == 5


def test_the_losing_streak_is_counted_in_trades():
    ps = [_p(-50, 1_800_000_100, pid=2, parent=1, reason="PARTIAL", entry=1_800_000_000), _p(-60, 1_800_000_200, pid=1, entry=1_800_000_000), _p(40, 1_800_000_300, pid=3)]
    assert metrics.summarize(ps)["overall"]["longest_losing_streak"] == 1       # rows 1+2 are one losing trade, then a win


def test_the_verdict_needs_30_trades_not_30_rows():
    ps = []
    for i in range(10):                                                          # 10 trades, each with a partial child = 20 rows
        e = 1_800_000_000 + i * 100 - 30
        ps += [_p(60, 1_800_000_000 + i * 100, pid=100 + i * 2, parent=1 + i * 2, reason="PARTIAL", entry=e), _p(60, 1_800_000_050 + i * 100, pid=1 + i * 2, entry=e)]
    m = metrics.summarize(ps)
    assert m["overall"]["trades"] == 10 and "only 10 trades" in " ".join(metrics.verdict(m)["reasons"])


def test_ids_that_restart_in_each_days_store_do_not_merge_trades():
    """Round 311: backtest/replay.py pools the trades of one in-memory store per day, and every store numbers its rows from 1."""
    day1, day2 = 1_800_000_000, 1_800_000_000 + 86_400
    a, b = _p(500, day1 + 100, pid=1), _p(-200, day2 + 100, pid=1)
    b.entry_ts = day2 + 40
    a.entry_ts = day1 + 40
    o = metrics.summarize([a, b])["overall"]
    assert o["trades"] == 2 and o["wins"] == 1 and o["net"] == 300


def test_a_partial_child_still_joins_its_parent_when_ids_repeat_across_days():
    day1, day2 = 1_800_000_000, 1_800_000_000 + 86_400
    parent1, child1 = _p(400, day1 + 200, pid=1), _p(-100, day1 + 100, pid=2, parent=1, reason="PARTIAL")
    parent2 = _p(-50, day2 + 200, pid=1)
    for x in (parent1, child1):
        x.entry_ts = day1 + 40
    parent2.entry_ts = day2 + 40
    o = metrics.summarize([parent1, child1, parent2])["overall"]
    assert o["trades"] == 2 and o["wins"] == 1
