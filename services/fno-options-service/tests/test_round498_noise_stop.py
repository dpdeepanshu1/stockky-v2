"""Round 498 (E518): review item 3 of the 13 Nov review ("the 25 % stop sits inside normal option-price noise"; a hypothesis, NOT_PROVEN).
The stop is never tighter than `sl_noise_atr_frac` x one daily ATR move of the OPTION (delta x ATR / premium), held between sl_pct and sl_max_pct; lots shrink so the rupee risk is unchanged."""
from __future__ import annotations

from dataclasses import fields

import pytest

import config
from control.catalog import CATALOG
from fno import gates, liveparams, paper, sizing, store as st
from fno.models import ContractRef
from fno.params import CONFIG_NAME, Params
from test_engine_paper import NOW, engine, make_cfg

P = Params()


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


# ── the formula ──
def test_a_cheap_option_gets_a_wider_stop_up_to_the_cap():
    assert sizing.noise_stop_pct(P, 100.0, 0.5, 300.0) == 30.0         # 0.2 x 0.5 x 300 / 100
    assert sizing.noise_stop_pct(P, 60.0, 0.5, 300.0) == 35.0          # 50 % would be wanted: capped at sl_max_pct
    assert sizing.noise_stop_pct(P, 150.0, 0.5, 300.0) == 25.0         # 20 %: the plain stop is already wider


def test_it_never_goes_below_the_plain_stop_and_a_low_cap_changes_nothing():
    assert sizing.noise_stop_pct(P.with_(sl_max_pct=20.0), 60.0, 0.5, 300.0) == 25.0
    assert sizing.noise_stop_pct(P.with_(sl_pct=40.0), 100.0, 0.5, 300.0) == 40.0


def test_off_or_missing_inputs_give_exactly_the_plain_stop():
    assert sizing.noise_stop_pct(P.with_(sl_noise_atr_frac=0.0), 60.0, 0.5, 300.0) == 25.0
    for prem, atr in ((60.0, None), (60.0, 0.0), (0.0, 300.0), (None, 300.0)):
        assert sizing.noise_stop_pct(P, prem, 0.5, atr) == 25.0, (prem, atr)


def test_delta_is_read_as_at_the_money_when_missing_and_is_clamped():
    assert sizing.noise_stop_pct(P, 100.0, None, 300.0) == sizing.noise_stop_pct(P, 100.0, 0.5, 300.0)
    assert sizing.noise_stop_pct(P, 100.0, -0.5, 300.0) == 30.0         # a put's delta is negative: its size counts
    assert sizing.noise_stop_pct(P, 1000.0, 0.01, 300.0) == 25.0        # 0.1 floor on delta: 0.2 x 0.1 x 300 / 1000 = 0.6 %: nothing
    assert sizing.noise_stop_pct(P, 100.0, 5.0, 300.0) == 35.0          # delta caps at 1: 60 % wanted, cap 35


def test_with_noise_stop_returns_the_same_params_when_nothing_widens_and_does_not_ratchet():
    assert sizing.with_noise_stop(P, 150.0, 0.5, 300.0) is P
    w = sizing.with_noise_stop(P, 100.0, 0.5, 300.0)
    assert w.sl_pct == 30.0 and P.sl_pct == 25.0                         # the input is untouched: the next entry starts from 25 again
    assert sizing.with_noise_stop(P, 100.0, 0.5, 300.0).sl_pct == 30.0


def test_the_rupee_risk_stays_inside_the_budget_and_lots_never_grow():
    shrank = False
    for cap in (300000.0, 500000.0, 1000000.0, 3000000.0, 10000000.0):
        q = P.with_(capital=cap, max_lots=1000)
        base = sizing.lots_for(100.0, 65, q)
        w = sizing.with_noise_stop(q, 100.0, 0.5, 300.0)
        wide = sizing.lots_for(100.0, 65, w)
        assert wide <= base
        shrank = shrank or wide < base
        assert 100.0 * 65 * wide * w.sl_pct / 100.0 <= q.capital * q.risk_per_trade_pct / 100.0 + 1e-6
    assert shrank


def test_the_position_stop_is_placed_on_the_widened_percentage():
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    w = sizing.with_noise_stop(P, 100.0, 0.5, 300.0)
    pos = paper.open_position(c, 1, 100.0, 0.0, w)
    assert pos.sl_price == paper.round_tick(70.0, 0.05, up=True)
    assert paper.evaluate_tick(pos, 74.0, w) is None                     # inside a 30 % stop, a plain 25 % one would have fired
    assert paper.evaluate_tick(pos, 69.9, w)[0] == paper.SL


# ── checks and wiring ──
def test_param_problems():
    assert gates.param_problems(P) == []
    assert any("FNO_SL_NOISE_ATR_FRAC" in w for w in gates.param_problems(P.with_(sl_noise_atr_frac=-0.1)))
    assert any("FNO_SL_NOISE_ATR_FRAC" in w for w in gates.param_problems(P.with_(sl_noise_atr_frac=3.0)))
    assert any("FNO_SL_MAX_PCT" in w for w in gates.param_problems(P.with_(sl_max_pct=100.0)))
    assert gates.param_problems(P.with_(sl_noise_atr_frac=0.0, sl_max_pct=0.0)) == []     # off: the cap is not read


def test_the_two_settings_are_wired_everywhere():
    names = {f.name for f in fields(Params)}
    for f, env, shipped in (("sl_noise_atr_frac", "FNO_SL_NOISE_ATR_FRAC", "0.2"), ("sl_max_pct", "FNO_SL_MAX_PCT", "35")):
        assert f in names and CONFIG_NAME[f] == env and hasattr(config, env)
        flag = next(x for x in CATALOG if x.key == env)
        assert flag.live and float(flag.shipped) == float(getattr(Params(), f))
    live = liveparams.current()
    assert live.sl_noise_atr_frac == 0.2 and live.sl_max_pct == 35.0
    assert Params.from_config(config).sl_max_pct == 35.0


# ── through the engine ──
def _open(db, **cfg):
    e = engine(db, cfg=make_cfg(FNO_UNDERLYINGS="NIFTY", **cfg))
    r = e.tick(NOW)
    assert len(r["entries"]) == 1, r
    return db.open_positions()[0]


def test_the_engine_widens_the_stop_and_takes_fewer_lots_with_the_floor_on(db):
    off = _open(db, FNO_SL_NOISE_ATR_FRAC=0.0)
    s2 = st.Store(":memory:")
    try:
        on = _open(s2, FNO_SL_NOISE_ATR_FRAC=1.0, FNO_SL_MAX_PCT=35.0)
    finally:
        s2.close()
    pct = lambda q: (1 - q.sl_price / q.entry_premium) * 100.0
    assert 24.0 <= pct(off) <= 26.0
    assert pct(on) > pct(off) + 3.0 and pct(on) <= 36.0
    assert on.lots <= off.lots
