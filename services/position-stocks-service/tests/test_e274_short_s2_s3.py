"""E274 round 44: S2 / S3 for a SHORT - the short facts (setup_inputs), their own stats cells, setup_short.decide and the plan_short privileges."""
import random
import types
from datetime import datetime, timezone, timedelta

import pytest

import config
from orders import setup_engine as eng, setup_inputs as si, setup_short, setup_stats, slot_ladder

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()
NOW = datetime(2026, 11, 2, 10, 30, tzinfo=IST)


def bar(minute, o, h, l, c, v=1000):
    return (DAY + minute * 60, o, h, l, c, v)


def noisy(n, base=100.0, seed=1):
    rnd = random.Random(seed)
    out, p = [], base
    for i in range(n):
        o = p
        p = base + rnd.uniform(-0.06, 0.06)
        out.append(bar(i, o, max(o, p) + 0.02, min(o, p) - 0.02, p))
    return out


def up_spike():
    rows = noisy(25)
    for k, c in enumerate((100.3, 100.6, 100.9, 101.2)):
        rows.append(bar(25 + k, c - 0.3, c + 0.05, c - 0.3, c, 1000))
    rows.append(bar(29, 101.2, 102.0, 101.15, 101.5, 6000))                 # the climax bar: a shooting star at the high
    rows.append(bar(30, 101.5, 101.9, 101.45, 101.5, 1500))
    return rows


NOW_TS = DAY + 32 * 60
TAPE_EXH = {"reliable": True, "exhaustion": "buy_exhaustion", "hvn_above": 101.7, "buy_pct_10m": 40.0}


# ---- cells ----------------------------------------------------------------
def test_cell_ids_keep_the_long_cells_and_split_s2_s3_shorts():
    assert setup_stats.cell_id("S2_RS_BREAKOUT", "LONG") == "S2_RS_BREAKOUT" and setup_stats.cell_id("S2_RS_BREAKOUT", "SHORT") == "S2_RS_BREAKOUT_SHORT"
    assert setup_stats.cell_id("S3_EXHAUSTION", "short") == "S3_EXHAUSTION_SHORT"
    assert setup_stats.cell_id("S4_CATALYST_DOWN", "SHORT") == "S4_CATALYST_DOWN" and setup_stats.cell_id("S1_CATALYST_UP", "LONG") == "S1_CATALYST_UP"
    assert setup_stats.cell_key({"id": "S2_RS_BREAKOUT", "slot": "0930", "side": "SHORT"}) == "S2_RS_BREAKOUT_SHORT|0930"
    assert setup_stats.cell_key({"id": "S2_RS_BREAKOUT", "slot": "0930"}) == "S2_RS_BREAKOUT|0930"          # an old stamp has no side = long
    assert setup_stats.cell_key(None) == "|later"


def _row(sid, side, pnl):
    import json
    return {"flow_json": json.dumps({"setup": {"id": sid, "slot": "0930", "side": side}}), "net_pnl": pnl, "entry_price": 100.0, "side": side, "opened_at": "2026-11-02T09:40:00"}


def test_summarize_keeps_a_short_s2_apart_from_a_long_s2():
    rows = [_row("S2_RS_BREAKOUT", "LONG", 10.0)] * 3 + [_row("S2_RS_BREAKOUT", "SHORT", -5.0)] * 2
    c = setup_stats.summarize(rows, min_trades=1)
    assert c["S2_RS_BREAKOUT|0930"]["n"] == 3 and c["S2_RS_BREAKOUT_SHORT|0930"]["n"] == 2 and "S2_RS_BREAKOUT_SHORT|ALL" in c


def test_demote_factor_reads_the_short_cell():
    ladder = {"demoted": {"S2_RS_BREAKOUT_SHORT|0930"}}
    assert slot_ladder.demote_factor("S2_RS_BREAKOUT", "0930", ladder, side="SHORT") < 1.0
    assert slot_ladder.demote_factor("S2_RS_BREAKOUT", "0930", ladder) == 1.0                    # the long cell is not demoted


# ---- pure measures --------------------------------------------------------
def test_spike_z_sign_picks_the_direction():
    down = noisy(25)
    for k, c in enumerate((99.7, 99.4, 99.1, 98.8)):                                          # a down-spike
        down.append(bar(25 + k, c + 0.3, c + 0.3, c - 0.05, c))
    assert si.spike_z(down)["z"] < -2.0 and si.spike_z(down, sign=-1)["z"] < -2.0
    up = si.spike_z(down, sign=1)                                                              # only up-moves of the noise remain: far below the S3 threshold
    assert up is None or up["z"] < eng.Z_SPIKE
    assert si.spike_z(up_spike(), sign=1)["z"] > eng.Z_SPIKE
    dn = si.spike_z(up_spike(), sign=-1)
    assert dn is None or dn["z"] > -eng.Z_SPIKE


def test_pullback_hold_short():
    end = DAY + 5 * 60
    rows = [bar(5, 99.9, 99.95, 99.5, 99.6), bar(6, 99.6, 99.7, 99.4, 99.5), bar(7, 99.5, 99.95, 99.45, 99.55), bar(8, 99.55, 99.6, 99.3, 99.4)]
    assert si.pullback_hold_short(rows, 99.8, end) is True                                      # broke below 99.8, rallied back to ~99.95, closes below
    assert si.pullback_hold_short(rows[:2], 99.8, end) is False and si.pullback_hold_short(rows, None, end) is False
    assert si.pullback_hold_short([bar(5, 99.9, 99.95, 99.5, 99.6), bar(6, 99.6, 99.7, 99.4, 99.9)], 99.8, end) is False        # closes back above


def test_structure_ok_short():
    ok = {"ms_trend": "range", "ms_bos_dir": None}
    assert si.structure_ok_short(ok, None) is True and si.structure_ok_short(ok, "no data") is False and si.structure_ok_short(None, None) is False
    assert si.structure_ok_short({"ms_trend": "bullish"}, None) is False
    assert si.structure_ok_short({"ms_trend": "range", "ms_bos_dir": "bullish", "ms_bos_bars": 3}, None) is False
    assert si.structure_ok_short({"ms_trend": "range", "ms_bos_dir": "bullish", "ms_bos_bars": 3, "ms_bos_failed": True}, None) is True
    assert si.structure_ok_short({"ms_trend": "bearish"}, None) is True


def test_tape_exhaustion_and_wall_for_a_short():
    assert si.tape_exhaustion_short({"reliable": True, "exhaustion": "buy_exhaustion"}) and si.tape_exhaustion_short({"reliable": True, "absorption": "buying_absorbed"})
    assert si.tape_exhaustion_short({"reliable": True, "checks": {"imbalance": "for"}})
    assert not si.tape_exhaustion_short({"reliable": False, "exhaustion": "buy_exhaustion"}) and not si.tape_exhaustion_short({"reliable": True, "exhaustion": "sell_exhaustion"})
    assert si.wall_close_short({"hvn_above": 101.7}, 101.5) and not si.wall_close_short({"hvn_above": 103.0}, 101.5) and not si.wall_close_short({"hvn_above": 101.0}, 101.5)
    assert not si.wall_close_short(None, 101.5)


def test_s3_facts_short_after_an_up_spike():
    f = si.s3_facts_short(up_spike(), price=101.5, vwap=100.0, atr_pct=None, tape=TAPE_EXH)
    assert f["z_move"] >= 2.0 and f["kind"] == "spike" and f["failure_candle"] and f["tape_exhaustion"] and f["vwap_stretch"] and f["climax_volume"] and f["wall_close"]
    assert si.s3_facts_short(noisy(10), price=100.0, vwap=100.0, atr_pct=None, tape=None) == {}      # window too short
    flat = si.s3_facts_short(noisy(40), price=100.0, vwap=100.0, atr_pct=None, tape=None)       # noise only: no up-spike beyond the threshold
    assert flat == {} or flat["z_move"] < eng.Z_SPIKE


def test_s3_short_fires_in_the_engine_with_two_confirmations():
    f = si.s3_facts_short(up_spike(), price=101.5, vwap=100.0, atr_pct=None, tape=TAPE_EXH)
    facts = setup_short.facts_short(symbol="X", price=101.5, now=NOW, event_polarity=0, orb=None, quote=None, gap_pct=None, inputs=f, allowed={eng.S3})
    r = eng.classify(facts)
    assert r["id"] == "S3_EXHAUSTION" and r["side"] == "SHORT" and r["size_mult"] == 0.5 and len(r["confirmations"]) >= 2
    f2 = dict(f, failure_candle=False)
    assert eng.classify(setup_short.facts_short(symbol="X", price=101.5, now=NOW, event_polarity=0, orb=None, quote=None, gap_pct=None, inputs=f2, allowed={eng.S3}))["id"] is None


def test_s2_facts_short_and_gather():
    orb = {"range_low": 99.9, "close_trigger": True, "trigger_close": 99.7}
    rows = noisy(25)
    rows.append(bar(25, 99.9, 99.92, 99.5, 99.55, 4000))                                         # a bearish bar below the range low (shooting-star style test below)
    f = si.s2_facts_short(rows, or_low=99.9, range_end_ts=DAY + 5 * 60, orb=orb, price=99.6)
    assert f["or_break"] == -1 and "pullback_hold" in f and f["vol_vs_norm"] > 1.0
    assert si.s2_facts_short(rows, or_low=99.9, range_end_ts=DAY + 5 * 60, orb={"close_trigger": False}, price=99.6)["or_break"] == 0
    g = si.gather_short(symbol="X", price=99.6, now_ts=NOW_TS, orb=orb, quote={}, gap_pct=-0.4, tape=None, want_s2=True, want_s3=False,
                        board_payload={"bearish": [{"symbol": "X"}], "bullish": []}, bars=rows, structure_summary={"ms_trend": "range"}, range_end_ts=DAY + 5 * 60)
    assert g["rs_pct"] == 0.0 and g["gap_pct"] == -0.4 and g["or_break"] == -1 and "structure_ok" in g and "z_move" not in g
    assert si.gather_short(symbol="X", price=1, now_ts=NOW_TS, orb=None, quote=None, gap_pct=None, tape=None, want_s2=False, want_s3=False) == {}


# ---- setup_short.decide ---------------------------------------------------
S2_INPUTS = {"rs_pct": 0.0, "gap_pct": -0.5, "structure_ok": True, "or_break": -1, "candle": -1, "vol_vs_norm": 2.0}
ORB = {"range_low": 99.9, "close_trigger": True, "trigger_close": 99.7}
TAPE_SELL = {"tape": {"reliable": True, "buy_pct_10m": 30.0}}
QUAL_NONE = types.SimpleNamespace(event_polarity=None)
QUAL_NEG = types.SimpleNamespace(event_polarity=-1)


def decide(**kw):
    base = dict(symbol="X", price=99.6, now=NOW, quality=QUAL_NONE, tape_json=TAPE_SELL, quote={"previous_close": 100.0, "avg_price": 100.0}, orb=ORB, gap_pct=-0.5, inputs=dict(S2_INPUTS), db=None)
    base.update(kw)
    return setup_short.decide(**base)


def test_a_weak_stock_breaking_the_range_low_matches_s2_without_a_news_event():
    r = decide()
    assert r["id"] == "S2_RS_BREAKOUT" and r["side"] == "SHORT" and r["stamp"]["side"] == "SHORT" and r["size_mult"] == 0.5 and "tape_with_side" in r["confirmations"]


def test_no_confirmations_no_setup_and_a_strong_stock_is_not_s2():
    assert decide(inputs=dict(S2_INPUTS, candle=0, vol_vs_norm=0.5), tape_json=None)["id"] is None
    assert decide(inputs=dict(S2_INPUTS, rs_pct=0.5))["id"] is None
    assert decide(inputs=dict(S2_INPUTS, gap_pct=0.5))["id"] is None                              # a gap UP is against a short


def test_the_switches():
    for name in ("SETUP_S2_S3_SHORT_ENABLED", "SETUP_S2_ENABLED"):
        old = getattr(config, name)
        try:
            setattr(config, name, False)
            assert decide()["id"] is None
        finally:
            setattr(config, name, old)
    old = config.SETUP_ENGINE_ENABLED
    try:
        config.SETUP_ENGINE_ENABLED = False
        assert decide()["skip"] == "OFF"
    finally:
        config.SETUP_ENGINE_ENABLED = old


def test_s4_still_wins_when_there_is_a_negative_event(monkeypatch):
    inp = dict(S2_INPUTS, sector_third="bottom")
    r = decide(quality=QUAL_NEG, price=99.0, inputs=inp, orb={"range_low": 99.9, "close_trigger": True, "trigger_close": 99.0}, gap_pct=-0.5)
    assert r["id"] == "S4_CATALYST_DOWN" and "S2_RS_BREAKOUT" in r.get("extra", [])


def test_s4_only_when_the_extra_setups_are_off_keeps_the_old_early_return(monkeypatch):
    monkeypatch.setattr(config, "SETUP_S2_S3_SHORT_ENABLED", False, raising=False)
    called = []
    monkeypatch.setattr(setup_short.setup_engine, "classify", lambda f: called.append(f) or {"id": None})
    assert decide(quality=QUAL_NONE)["id"] is None and called == []                              # no negative event and no S2 / S3 = nothing to read


def test_the_gather_runs_once_when_the_inputs_do_not_carry_the_facts(monkeypatch):
    seen = []
    monkeypatch.setattr(si, "gather_short", lambda **k: (seen.append(k) or dict(S2_INPUTS)))
    r = decide(inputs={})
    assert r["id"] == "S2_RS_BREAKOUT" and len(seen) == 1 and seen[0]["want_s2"] is True and seen[0]["want_s3"] is True


def test_a_demoted_short_cell_shrinks_the_size_and_a_promoted_one_is_full(monkeypatch):
    class L:
        pass
    monkeypatch.setattr(slot_ladder, "get", lambda db: {"promoted": set(), "demoted": {"S2_RS_BREAKOUT_SHORT|later"}, "cells": {}})
    half = decide(db=object())["size_mult"]
    monkeypatch.setattr(slot_ladder, "get", lambda db: {"promoted": {"S2_RS_BREAKOUT|later"}, "demoted": set(), "cells": {}})
    assert decide(db=object())["size_mult"] == 0.5                                               # the LONG S2 cell being promoted gives a short nothing
    monkeypatch.setattr(slot_ladder, "get", lambda db: {"promoted": {"S2_RS_BREAKOUT_SHORT|later"}, "demoted": set(), "cells": {}})
    assert decide(db=object())["size_mult"] == 1.0 and half == 0.25


def test_junk_never_raises():
    assert setup_short.decide(symbol="X", price="x", now="y", quality=7, tape_json=3, quote=1, orb=2, gap_pct="z", inputs=4)["id"] is None


# ---- plan_short privileges: only S4 earns them ------------------------------
def test_plan_short_gives_the_early_window_the_board_skip_and_the_squeeze_exemption_to_s4_only():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "orders" / "short_entry.py").read_text()
    assert '_is_s4 = _setup.get("id") == "S4_CATALYST_DOWN"' in src and "if _skip_board and not _is_s4:" in src and "if early and not _is_s4:" in src and "s4=_is_s4)" in src


def test_the_short_target_may_learn_from_a_short_s2_cell(monkeypatch):
    from orders import short_target as st
    cell = {"n": 40, "gain_n": 20, "typical_gain_pct": 0.9, "ttp_n": 20, "ttp_min": 12.0}
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S2_RS_BREAKOUT_SHORT|0930": cell, "S2_RS_BREAKOUT|0930": dict(cell, typical_gain_pct=5.0)})
    lg = st.learned_gain({"id": "S2_RS_BREAKOUT", "slot": "0930"}, NOW, object(), 2.0)
    assert lg and lg["cell"] == "S2_RS_BREAKOUT_SHORT|0930" and lg["gain_pct"] < 2.0
