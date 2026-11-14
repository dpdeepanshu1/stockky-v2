"""Round 495 (E515): CORE must not trade AGAINST its intraday score; NIFTY and SENSEX are one bet; any exit starts a re-entry pause; the trail keeps more of a winner (items 2, 4, 5, 6 of the 13 Nov review)."""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

import config
from control.catalog import CATALOG
from fno import gates, liveparams, paper, signals, store as st
from fno.models import ContractRef
from fno.params import CONFIG_NAME, Params
from fno.signals import DOWN, FLAT, UP
from test_engine_paper import NOW, FakeProvider, engine, make_cfg

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 13, 11, 0, tzinfo=IST)
EXPIRY = datetime(2026, 11, 17).date()


# ── item 4: CORE direction ──
def _bias(d, i, c=0.0, **kw):
    return signals.combine_bias(d, [], 20.0, i, [], c, [], Params().with_(**kw))


def test_a_rising_daily_trend_cannot_buy_a_call_while_the_intraday_score_is_down():
    b = _bias(60.0, -10.0)                                 # 60 - 10 = 50 >= 45: the old rule bought a CALL here
    assert b["raw_direction"] == UP and b["direction"] == FLAT
    assert any("against the UP lean" in r for r in b["reasons"])


def test_a_falling_daily_trend_cannot_buy_a_put_while_the_intraday_score_is_up():
    b = _bias(-60.0, 10.0)
    assert b["raw_direction"] == DOWN and b["direction"] == FLAT


def test_the_chain_cannot_carry_a_lean_against_the_intraday_score_either():
    assert _bias(30.0, -5.0, c=25.0)["direction"] == FLAT


def test_agreeing_or_silent_intraday_part_still_trades():
    assert _bias(40.0, 10.0)["direction"] == UP and _bias(-40.0, -10.0)["direction"] == DOWN
    assert _bias(50.0, 0.0)["direction"] == UP and _bias(-50.0, 0.0)["direction"] == DOWN


def test_score_and_parts_unchanged_and_switch_off_is_the_old_behaviour():
    on, off = _bias(60.0, -10.0), _bias(60.0, -10.0, core_intraday_agree=False)
    assert on["score"] == off["score"] == 50.0 and on["parts"] == off["parts"]
    assert off["direction"] == UP and not any("no CORE entry" in r for r in off["reasons"])


def test_below_the_threshold_raw_equals_direction():
    b = _bias(20.0, -10.0)
    assert b["direction"] == FLAT and b["raw_direction"] == FLAT


# ── item 5: one bet across both indices ──
def _gate(state, key, p=None):
    return gates.entry_gates(T0, EXPIRY, 14.0, 0.0, state, p or Params().with_(max_open_positions=5), key=key)


def test_a_call_on_nifty_blocks_a_call_on_sensex():
    ok, why = _gate(gates.DayState(open_positions=1, open_same_side={"NIFTY:CE": 1}), "SENSEX:CE")
    assert not ok and any("one bet" in w for w in why)


def test_the_other_side_on_the_other_index_is_not_this_rules_business():
    ok, why = _gate(gates.DayState(open_positions=1, open_same_side={"NIFTY:CE": 1}), "SENSEX:PE")
    assert not any("one bet" in w for w in why)


def test_the_cap_is_configurable_and_zero_turns_it_off():
    st_ = gates.DayState(open_positions=1, open_same_side={"NIFTY:CE": 1})
    assert _gate(st_, "SENSEX:CE", Params().with_(max_open_positions=5, max_same_dir_open=2))[0]
    assert _gate(st_, "SENSEX:CE", Params().with_(max_open_positions=5, max_same_dir_open=0))[0]


# ── item 6: re-entry pause after ANY exit ──
def test_any_exit_pauses_the_same_index_and_side():
    s = gates.DayState(last_exit_by_key={"NIFTY:CE": T0.timestamp() - 600})
    ok, why = _gate(s, "NIFTY:CE")
    assert not ok and any("cooling down after the last NIFTY:CE exit" in w for w in why)


def test_other_keys_and_old_exits_and_zero_are_free():
    s = gates.DayState(last_exit_by_key={"NIFTY:CE": T0.timestamp() - 600})
    assert _gate(s, "NIFTY:PE")[0] and _gate(s, "SENSEX:CE")[0]
    assert _gate(gates.DayState(last_exit_by_key={"NIFTY:CE": T0.timestamp() - 31 * 60}), "NIFTY:CE")[0]
    assert _gate(s, "NIFTY:CE", Params().with_(max_open_positions=5, reentry_cooldown_min=0))[0]


def _closed(db, und, ot, reason, ts):
    c = ContractRef(und, "2026-11-17", 24500.0, ot, 65, None, "", 0.05)
    pos = paper.open_position(c, 1, 100.0, ts - 600, Params(), mode="DEMO")
    pos.id = db.insert_position(pos)
    paper.close_position(pos, 104.0, ts, reason, Params())
    db.update_position(pos)


def test_the_store_reports_the_last_exit_per_key_except_squareoff():
    db = st.Store(":memory:")
    t = datetime(2026, 11, 13, 10, 0, tzinfo=IST).timestamp()
    _closed(db, "NIFTY", "CE", paper.TRAIL, t)
    _closed(db, "NIFTY", "CE", paper.MANUAL, t + 300)
    _closed(db, "NIFTY", "CE", paper.SQUAREOFF, t + 900)
    _closed(db, "SENSEX", "PE", paper.SQUAREOFF, t + 60)
    assert db.day_summary("2026-11-13", "DEMO")["last_exit_by_key"] == {"NIFTY:CE": t + 300}
    db.close()


# ── wiring ──
def test_the_three_rules_are_wired_everywhere():
    names = {f.name for f in fields(Params)}
    for f, env in (("core_intraday_agree", "FNO_CORE_INTRADAY_AGREE"), ("max_same_dir_open", "FNO_MAX_SAME_DIRECTION_OPEN"), ("reentry_cooldown_min", "FNO_REENTRY_COOLDOWN_MIN")):
        assert f in names and CONFIG_NAME[f] == env and hasattr(config, env)
        flag = next(x for x in CATALOG if x.key == env)
        assert flag.live and flag.shipped == str(getattr(Params(), f)).lower()
    live = liveparams.current()
    assert live.core_intraday_agree is True and live.max_same_dir_open == 1 and live.reentry_cooldown_min == 30


# ── the live engine ──
@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def _falling(db, **cfg):
    class P(FakeProvider):
        def intraday(self, u):
            from helpers import make_intraday
            return make_intraday(step=-6.0)            # falling session; the daily candles are rising
    return engine(db, P(), make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True, FNO_SIGNAL_MIN_SCORE=2.0, **cfg))


def test_engine_buys_the_call_with_the_rule_off_and_nothing_with_it_on(db):
    off = _falling(db, FNO_CORE_INTRADAY_AGREE=False).tick(NOW)
    assert off["decisions"]["NIFTY"]["parts"]["intraday"] < 0 and len(off["entries"]) == 1
    db2 = st.Store(":memory:")
    on = _falling(db2, FNO_CORE_INTRADAY_AGREE=True).tick(NOW)
    assert on["entries"] == [] and db2.open_positions() == [] and on["decisions"]["NIFTY"]["direction"] == FLAT
    db2.close()


def test_a_held_call_is_still_flipped_by_the_raw_lean(db, monkeypatch):
    from fno import strategies
    real = strategies.core_bias
    monkeypatch.setattr(strategies, "core_bias", lambda x: {**real(x), "direction": FLAT, "raw_direction": DOWN, "score": -50.0})
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True))
    pos = paper.open_position(ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05), 1, 205.0, NOW.timestamp(), e.params())
    pos.id = db.insert_position(pos)
    out = e.tick(NOW)
    assert pos.id in out["exits"] and db.get_position(pos.id).exit_reason == paper.SIGNAL_FLIP and out["entries"] == []


def test_engine_does_not_rebuy_the_same_minute_a_position_exits(db):
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=True))
    pos = paper.open_position(ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05), 1, 100.0, NOW.timestamp(), e.params())
    pos.id = db.insert_position(pos)
    out = e.tick(NOW)                                   # the fake chain prices it far above the target: TARGET exit this cycle
    assert pos.id in out["exits"] and out["entries"] == []
    assert any("cooling down after the last NIFTY:CE exit" in " ".join(x.get("why", [])) for x in db.events("skip"))