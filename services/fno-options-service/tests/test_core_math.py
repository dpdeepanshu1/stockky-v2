"""F&O rounds 2-3: Black-Scholes, chain analytics, contract choice, gates, sizing, costs. Offline."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from fno import analytics, bs, costs, gates, signals, sizing
from fno.models import Chain, Side
from fno.params import Params
from helpers import make_chain, make_daily, make_intraday

NOW = datetime(2026, 11, 10, 10, 30)


# ---- Black-Scholes -----------------------------------------------------------------------------------------------------------
def test_put_call_parity():
    s, k, t, r, sg = 24500.0, 24600.0, 7 / 365, 0.065, 0.14
    c, p = bs.price(s, k, t, r, sg, "CE"), bs.price(s, k, t, r, sg, "PE")
    assert c - p == pytest.approx(s - k * __import__("math").exp(-r * t), abs=1e-6)


def test_known_value_atm_call():
    # S=K=100, T=1, r=5%, sigma=20% -> 10.4506 (textbook value)
    assert bs.price(100, 100, 1.0, 0.05, 0.20, "CE") == pytest.approx(10.4506, abs=1e-3)
    assert bs.price(100, 100, 1.0, 0.05, 0.20, "PE") == pytest.approx(5.5735, abs=1e-3)


def test_delta_bounds_and_signs():
    g_ce = bs.greeks(24500, 24500, 5 / 365, 0.065, 0.14, "CE")
    g_pe = bs.greeks(24500, 24500, 5 / 365, 0.065, 0.14, "PE")
    assert 0.45 < g_ce["delta"] < 0.58 and -0.58 < g_pe["delta"] < -0.42
    assert g_ce["theta"] < 0 and g_ce["vega"] > 0 and g_ce["gamma"] > 0


def test_implied_vol_round_trip():
    px = bs.price(24500, 24700, 6 / 365, 0.065, 0.17, "CE")
    assert bs.implied_vol(px, 24500, 24700, 6 / 365, 0.065, "CE") == pytest.approx(0.17, abs=1e-3)


def test_implied_vol_rejects_impossible_prices():
    assert bs.implied_vol(0.0, 100, 100, 0.1, 0.05, "CE") is None
    assert bs.implied_vol(1.0, 150, 100, 0.1, 0.05, "CE") is None        # below intrinsic
    assert bs.implied_vol(10.0, 100, 100, 0.0, 0.05, "CE") is None        # no time left


def test_year_fraction_expiry_close():
    assert bs.year_fraction(datetime(2026, 11, 17, 15, 30), date(2026, 11, 17)) == 0.0
    assert bs.year_fraction(datetime(2026, 11, 17, 9, 15), date(2026, 11, 17)) == pytest.approx(6.25 * 3600 / (365 * 86400), rel=1e-6)


# ---- analytics ---------------------------------------------------------------------------------------------------------------
def test_chain_model_atm_and_step():
    ch = make_chain(spot=24512)
    assert ch.atm_strike() == 24500 and ch.step() == 50
    assert Chain.from_json(ch.to_json()).atm_strike() == 24500           # JSON round trip


def test_pcr_and_walls_and_max_pain():
    ch = make_chain(spot=24500, pe_oi_bias=1.5)
    s = analytics.chain_summary(ch)
    assert s["pcr_oi"] == pytest.approx(1.5, abs=0.01)
    assert s["call_wall"] >= 24500 and s["put_wall"] <= 24500
    assert s["max_pain"] is not None and abs(s["max_pain"] - 24500) <= 100


def test_buildup_codes():
    mk = lambda ltp, pc, oi, poi: Side(ltp=ltp, prev_close=pc, oi=oi, prev_oi=poi)
    assert analytics.buildup(mk(110, 100, 120, 100)) == 2      # price up, OI up
    assert analytics.buildup(mk(110, 100, 90, 100)) == 1       # price up, OI down
    assert analytics.buildup(mk(90, 100, 90, 100)) == -1       # price down, OI down
    assert analytics.buildup(mk(90, 100, 120, 100)) == -2      # price down, OI up
    assert analytics.buildup(Side(ltp=1)) is None


def test_atm_block_straddle_and_expected_move():
    ch = make_chain(spot=24500)
    a = analytics.atm_block(ch)
    assert a["atm_strike"] == 24500 and a["straddle"] > 0 and 0.5 < a["expected_move_pct"] < 6


def test_iv_rank_needs_history():
    assert analytics.iv_rank(14, [12.0] * 5) is None
    r = analytics.iv_rank(15, [10 + i * 0.1 for i in range(40)])
    assert r and 0 <= r["iv_rank"] <= 100


def test_chain_score_bullish_and_bearish():
    bull, _ = analytics.chain_score({"pcr_oi": 1.4, "spot": 24500, "put_wall": 24480, "call_wall": 25000, "flow": 0.6})
    bear, _ = analytics.chain_score({"pcr_oi": 0.6, "spot": 24500, "call_wall": 24520, "put_wall": 24000, "flow": -0.6})
    assert bull > 20 and bear < -20
    assert analytics.chain_score({})[0] == 0.0                # missing data adds nothing


# ---- direction ---------------------------------------------------------------------------------------------------------------
def test_bias_up_in_a_clean_uptrend():
    b = signals.index_bias(make_daily(drift=0.002), make_intraday(step=6.0), None, Params(min_score=30))
    assert b["direction"] == signals.UP and b["score"] > 30


def test_bias_down_in_a_clean_downtrend():
    b = signals.index_bias(make_daily(drift=-0.002, start=26000), make_intraday(step=-6.0), None, Params(min_score=30))
    assert b["direction"] == signals.DOWN and b["score"] < -30


def test_bias_flat_without_data():
    b = signals.index_bias([], [], None, Params())
    assert b["direction"] == signals.FLAT and b["score"] == 0.0


def test_to_ms_bars_drops_bad_rows_and_sorts():
    rows = [{"t": 2, "o": 1, "h": 2, "l": 1, "c": 2}, {"t": 1, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 5}, {"t": 3}, [4, 1, 2, 1, 2, 9]]
    out = signals.to_ms_bars(rows)
    assert [r[0] for r in out] == [1, 2, 4]


# ---- contract choice ---------------------------------------------------------------------------------------------------------
def test_pick_ce_near_target_delta():
    ch = make_chain(spot=24500)
    r = signals.pick_contract(ch, signals.UP, NOW, Params(min_oi=1, min_volume=1), lot_size=65)
    assert r["ok"] and r["contract"].opt_type == "CE" and r["contract"].lot_size == 65
    assert 0.35 <= r["delta"] <= 0.65 and r["premium"] > 0


def test_pick_pe_for_down_and_none_for_flat():
    ch = make_chain(spot=24500)
    assert signals.pick_contract(ch, signals.DOWN, NOW, Params(min_oi=1, min_volume=1), lot_size=65)["contract"].opt_type == "PE"
    assert signals.pick_contract(ch, signals.FLAT, NOW)["ok"] is False


def test_pick_rejects_wide_spread_and_missing_lot():
    ch = make_chain(spot=24500)
    for row in ch.rows:
        for s in (row.ce, row.pe):
            s.ask = s.bid * 1.2                      # 18 % spread everywhere
    r = signals.pick_contract(ch, signals.UP, NOW, Params(min_oi=1, min_volume=1), lot_size=65)
    assert not r["ok"] and any("spread" in (c["reject"] or "") for c in r["considered"])
    r2 = signals.pick_contract(make_chain(), signals.UP, NOW, Params(min_oi=1, min_volume=1), lot_size=None)
    assert not r2["ok"] and "lot size unknown" in r2["reasons"]


def test_delta_falls_back_to_black_scholes_when_missing():
    ch = make_chain(spot=24500)
    for row in ch.rows:
        for s in (row.ce, row.pe):
            s.delta = None
    r = signals.pick_contract(ch, signals.UP, NOW, Params(min_oi=1, min_volume=1), lot_size=65)
    assert r["ok"] and 0.35 <= r["delta"] <= 0.65


# ---- gates -------------------------------------------------------------------------------------------------------------------
def test_gates_pass_in_a_normal_session():
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 3.0, gates.DayState(), Params())
    assert ok and why == []


@pytest.mark.parametrize("kw,needle", [
    (dict(now=datetime(2026, 11, 14, 10, 30)), "not a trading day"),                  # Saturday
    (dict(now=datetime(2026, 11, 10, 9, 20)), "outside the entry window"),
    (dict(now=datetime(2026, 11, 10, 14, 45)), "outside the entry window"),
    (dict(expiry=date(2026, 11, 10)), "expiry day"),
    (dict(vix=31.0), "VIX"),
    (dict(vix=None), "VIX unknown"),
    (dict(age=60.0), "old"),
    (dict(state=gates.DayState(trades_today=3)), "trades today"),
    (dict(state=gates.DayState(open_positions=2)), "positions open"),
    (dict(state=gates.DayState(realized_pnl_net=-16000)), "daily loss"),
    (dict(state=gates.DayState(open_same_side={"NIFTY:CE": 1}), key="NIFTY:CE"), "already holding"),
])
def test_each_gate_blocks(kw, needle):
    ok, why = gates.entry_gates(kw.get("now", NOW), kw.get("expiry", date(2026, 11, 17)), kw.get("vix", 14.0), kw.get("age", 3.0),
                                kw.get("state", gates.DayState()), Params(), key=kw.get("key", ""))
    assert not ok and any(needle in w for w in why), why


def test_cooldown_after_stopout():
    st = gates.DayState(last_stopout_ts=NOW.timestamp() - 120)
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 3.0, st, Params())
    assert not ok and any("cooling" in w for w in why)


def test_holiday_blocks():
    ok, why = gates.entry_gates(NOW, date(2026, 11, 17), 14.0, 3.0, gates.DayState(), Params(), holidays=["2026-11-10"])
    assert not ok and "not a trading day" in why


# ---- sizing ------------------------------------------------------------------------------------------------------------------
def test_sizing_respects_risk_budget_and_caps():
    p = Params(capital=200000, risk_per_trade_pct=1.0, sl_pct=25, max_lots=5)
    # budget 2000; premium 100 x lot 65 x 25% = 1625 per lot -> 1 lot
    assert sizing.lots_for(100, 65, p) == 1
    # premium 40: 650 per lot -> 3 lots by risk; cash cap 0.35*200000/(40*65)=26 -> 3
    assert sizing.lots_for(40, 65, p) == 3
    assert sizing.lots_for(40, 65, p.with_(max_lots=2)) == 2


def test_sizing_skips_when_one_lot_does_not_fit():
    p = Params(capital=100000, risk_per_trade_pct=0.5, sl_pct=25)
    assert sizing.lots_for(300, 65, p) is None                  # 4875 risk per lot vs 500 budget
    assert sizing.lots_for(0, 65, p) is None and sizing.lots_for(100, 0, p) is None


# ---- costs -------------------------------------------------------------------------------------------------------------------
def test_costs_stt_only_on_sell_and_stamp_only_on_buy():
    p = Params()
    c = costs.round_trip("NIFTY", 100.0, 150.0, 65, p)
    assert c.stt == pytest.approx(150.0 * 65 * 0.15 / 100.0, abs=0.01)
    assert c.stamp == pytest.approx(100.0 * 65 * 0.003 / 100.0, abs=0.01)
    assert c.brokerage == pytest.approx(40.0, abs=0.01)                       # Rs 20 per leg (cap not hit on 6500+ turnover)
    assert c.total > c.brokerage + c.stt


def test_costs_scale_and_breakeven():
    p = Params()
    small = costs.breakeven_move_pct("NIFTY", 20.0, 65, p)
    big = costs.breakeven_move_pct("NIFTY", 200.0, 65, p)
    assert small > big > 0                                                    # fixed charges hurt cheap premiums more
    assert costs.round_trip("SENSEX", 100, 100, 20, p).exchange != costs.round_trip("NIFTY", 100, 100, 20, p).exchange
