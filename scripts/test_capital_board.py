"""Tests for capital_board.py (the three-way split of one Dhan account: 25 % each fixed + 25 % bonus to the best net return; inactive services give their part back; the file board)."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "real-trade-service"))
import capital_board as cb  # noqa: E402


def st(pnl, cap=100000, trades=30, **kw):
    d = {"net_pnl": pnl, "capital_base": cap, "trades": trades}
    d.update(kw)
    return d


def total(r):
    return abs(sum(r.values()) - 100) < 1e-3


def test_no_evidence_means_equal_thirds():
    assert cb.allocate({}) == {"real_trade": 33.3333, "position_stocks": 33.3333, "fno": 33.3333}


def test_winner_mode_gives_bonus_to_best_return():
    r = cb.allocate({"real_trade": st(1000), "position_stocks": st(3000), "fno": st(-500)}, bonus_mode="winner")
    assert r == {"real_trade": 25.0, "position_stocks": 50.0, "fno": 25.0}


def test_proportional_split():
    r = cb.allocate({"real_trade": st(1000), "position_stocks": st(3000), "fno": st(-500)})
    assert total(r) and r["fno"] == 25.0 and r["position_stocks"] > r["real_trade"] > 25.0


def test_score_is_return_on_capital_not_rupees():
    r = cb.allocate({"real_trade": st(5000, cap=1_000_000), "position_stocks": st(0), "fno": st(2000, cap=100000)}, bonus_mode="winner")
    assert r["fno"] == 50.0 and r["real_trade"] == 25.0


def test_too_few_trades_or_frozen_gets_no_bonus():
    r = cb.allocate({"real_trade": st(1000, trades=5), "position_stocks": st(10, trades=40), "fno": st(9000)}, bonus_mode="winner", frozen=["fno"])
    assert r["position_stocks"] == 50.0 and r["fno"] == 25.0 and r["real_trade"] == 25.0


def test_inactive_service_gets_zero_and_its_part_is_shared():
    r = cb.allocate({}, active=["real_trade", "position_stocks"])
    assert r == {"real_trade": 50.0, "position_stocks": 50.0, "fno": 0.0}
    r = cb.allocate({"real_trade": st(100), "position_stocks": st(50)}, active=["real_trade", "position_stocks"], bonus_mode="winner")
    assert r["fno"] == 0.0 and total(r) and r["real_trade"] == 50.0       # 37.5 + 25 bonus capped at 50, the excess goes to the other active one
    assert r["position_stocks"] == 50.0
    assert cb.allocate({}, active=["real_trade"]) == {"real_trade": 100.0, "position_stocks": 0.0, "fno": 0.0}


def test_inactive_never_wins_the_bonus():
    r = cb.allocate({"fno": st(9000), "real_trade": st(10)}, active=["real_trade", "position_stocks"], bonus_mode="winner")
    assert r["fno"] == 0.0


def test_max_pct_cap_redistributes_to_active_only():
    r = cb.allocate({"real_trade": st(1000)}, bonus_mode="winner", max_pct=30.0)
    assert r["real_trade"] == 33.3333 and total(r)           # cap can never be below an equal split of the active ones


def test_step_limit_and_bad_input_never_raise():
    prev = {"real_trade": 33.3333, "position_stocks": 33.3333, "fno": 33.3333}
    r = cb.allocate({"real_trade": st(1000), "position_stocks": st(-1), "fno": st(-1)}, bonus_mode="winner", previous=prev, max_step_pct=5)
    assert total(r) and abs(r["real_trade"] - 33.3333) <= 5.0001
    assert total(cb.allocate({"real_trade": {"net_pnl": "x"}, "fno": None}))
    assert cb.amounts(None, {"fno": 25.0}) == {"fno": 0.0}
    assert cb.norm_mode("LIVE ") == "live" and cb.norm_mode("lvie") == "off" and cb.norm_mode(None) == "off"


def test_files_roundtrip_and_staleness(tmp_path):
    d = str(tmp_path)
    assert cb.write_stats(d, "fno", net_pnl=10.5, trades=3, capital_base=1000, exposure=250.0, active=True, mode="shadow")
    assert cb.read_stats(d, "fno")["exposure"] == 250.0 and cb.exposure_of(d, "fno") == 250.0
    assert cb.read_stats(d, "fno", max_age_s=60)["trades"] == 3
    cb.write_stats(d, "fno", net_pnl=0, trades=0, capital_base=0, exposure=250.0, active=True, now=time.time() - 5000)
    assert cb.exposure_of(d, "fno", 900) == 0.0                           # an old figure is not counted
    assert cb.read_stats(str(tmp_path / "nope"), "fno") is None
    assert not cb.write_stats(d, "bogus", net_pnl=0, trades=0, capital_base=0, exposure=0, active=True)


def test_refresh_decides_once_per_day_and_updates_amounts(tmp_path):
    d = str(tmp_path)
    cb.write_stats(d, "real_trade", net_pnl=2000, trades=30, capital_base=100000, exposure=1000, active=True)
    cb.write_stats(d, "position_stocks", net_pnl=0, trades=30, capital_base=100000, exposure=2000, active=True)
    cb.write_stats(d, "fno", net_pnl=0, trades=0, capital_base=0, exposure=0, active=False)
    a = cb.refresh(d, mode="shadow", day="2026-11-09", free_cash=7000)
    assert a["active"] == ["real_trade", "position_stocks"] and a["pcts"]["fno"] == 0.0 and a["total_equity"] == 10000.0
    assert a["pcts"]["real_trade"] == 50.0 and a["amounts"]["real_trade"] == 5000.0
    # same day: percentages stay, rupee amounts follow the new cash; a later stats change does not re-decide
    cb.write_stats(d, "position_stocks", net_pnl=99999, trades=30, capital_base=100000, exposure=2000, active=True)
    b = cb.refresh(d, mode="shadow", day="2026-11-09", free_cash=17000)
    assert b["pcts"] == a["pcts"] and b["total_equity"] == 20000.0 and b["decided_ts"] == a["decided_ts"]
    # F&O turns active: the set changed, so it is re-decided the same day
    cb.write_stats(d, "fno", net_pnl=0, trades=0, capital_base=0, exposure=0, active=True)
    c = cb.refresh(d, mode="shadow", day="2026-11-09", free_cash=17000)
    assert "fno" in c["active"] and c["pcts"]["fno"] > 0 and total(c["pcts"])
    assert cb.refresh(d, mode="off", day="2026-11-09", free_cash=1) is None


def test_effective_share_only_when_both_modes_live_and_board_fresh(tmp_path):
    d = str(tmp_path)
    cb.write_stats(d, "fno", net_pnl=0, trades=0, capital_base=0, exposure=0, active=True)
    cb.refresh(d, mode="live", day="2026-11-09", free_cash=1000)
    assert cb.effective_share(d, "real_trade", "live", 50.0)[0] == 33.3333
    assert cb.effective_share(d, "real_trade", "shadow", 50.0) == (50.0, "fixed (mode not live)")
    assert cb.effective_share(d, "real_trade", "off", 50.0)[0] == 50.0
    cb.refresh(d, mode="shadow", day="2026-11-09", free_cash=1000)
    assert cb.effective_share(d, "real_trade", "live", 50.0) == (50.0, "fixed (board not live)")      # the board's own mode must be live too
    assert cb.effective_share(str(tmp_path / "missing"), "real_trade", "live", 50.0) == (50.0, "fixed (no fresh board)")


def test_amount_for_needs_a_fresh_board(tmp_path):
    d = str(tmp_path)
    assert cb.amount_for(d, "fno")[0] is None
    cb.write_stats(d, "fno", net_pnl=0, trades=0, capital_base=0, exposure=0, active=True)
    cb.refresh(d, mode="shadow", day="2026-11-09", free_cash=12000)
    assert cb.amount_for(d, "fno")[0] == 4000.0                          # 3 active, 25 % + equal bonus = 33.33 % of 12000
    cb.write_json(d, cb.ALLOC_FILE, {"ts": time.time() - 10 * 3600, "pcts": {"fno": 25.0}, "amounts": {"fno": 5000.0}})
    assert cb.amount_for(d, "fno", 6 * 3600)[0] is None
