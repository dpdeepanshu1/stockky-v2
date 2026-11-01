"""E273 round 5: S2 / S3 facts, sector third, priced_in and the NO_SETUP refusal. Plain runner: python3 tests/test_e273_setup_s2_s3.py"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders import setup_engine as se, setup_inputs as si, setup_live as sl

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()           # 09:15 IST


def at(h, m):
    return datetime(2026, 11, 2, h, m, tzinfo=IST)


def bar(minute, o, h, l, c, v=1000):
    return (DAY + minute * 60, o, h, l, c, v)                         # minute 0 = 09:15


def noisy(n, base=100.0, seed=1):
    rnd = random.Random(seed)
    out, p = [], base
    for i in range(n):
        o = p
        p = base + rnd.uniform(-0.06, 0.06)
        out.append(bar(i, o, max(o, p) + 0.02, min(o, p) - 0.02, p))
    return out


# ---- pure measures --------------------------------------------------------------------------------------------------------------------------------------

def test_candle_signal():
    assert si.candle_signal([bar(0, 100, 100.1, 99.0, 100.05)]) == 1                       # hammer
    assert si.candle_signal([bar(0, 100, 101.0, 99.95, 99.98)]) == -1                      # shooting star
    assert si.candle_signal([bar(0, 100.5, 100.6, 99.9, 100.0), bar(1, 99.95, 100.8, 99.9, 100.7)]) == 1   # bullish engulfing
    assert si.candle_signal([bar(0, 100, 100.25, 99.95, 100.2)]) == 0 and si.candle_signal([]) == 0


def test_closed_bars_drop_the_forming_bar_and_junk():
    rows = si.closed_bars([bar(0, 1, 1, 1, 1), bar(1, 1, 1, 1, 1), ("x",), None], DAY + 90)
    assert len(rows) == 1                                                                  # minute 1 is still forming at +90 s


def test_vol_vs_norm_needs_history():
    rows = [bar(i, 100, 100.1, 99.9, 100, 1000) for i in range(6)] + [bar(6, 100, 100.1, 99.9, 100, 3000)]
    assert si.vol_vs_norm(rows) == 3.0
    assert si.vol_vs_norm(rows[:4]) is None


def test_pullback_hold():
    end = DAY + 5 * 60
    rows = [bar(5, 99.9, 100.4, 99.9, 100.3), bar(6, 100.3, 100.5, 100.05, 100.4), bar(7, 100.4, 100.7, 100.35, 100.6)]
    assert si.pullback_hold(rows, 100.0, end) is True
    assert si.pullback_hold(rows[:1], 100.0, end) is False                                 # broke, no dip yet
    assert si.pullback_hold([bar(5, 99.9, 100.4, 99.9, 100.3), bar(6, 100.3, 100.4, 100.3, 100.35)], 100.0, end) is False   # never came back to the high
    assert si.pullback_hold(rows, None, end) is False


def test_spike_z_needs_enough_returns_and_sees_a_down_spike():
    assert si.spike_z(noisy(10)) is None
    rows = noisy(25)
    for k in range(1, 9):                                                                  # a steady 0.3 % per minute fall
        p = rows[-1][4]
        rows.append(bar(24 + k, p, p + 0.01, p * 0.997 - 0.01, p * 0.997))
    sp = si.spike_z(rows)
    assert sp and sp["z"] <= -2.0 and sp["k"] >= 2
    flat = noisy(40)
    assert abs(si.spike_z(flat)["z"]) < 4.0                                                # pure noise: modest


def test_priced_in():
    assert si.priced_in(103.0, 100.0, 1.0, +1, 2.0) == 1.5                                 # +3 % against an expected 2 %
    assert si.priced_in(101.0, 100.0, 1.0, +1, 2.0) == 0.5
    assert si.priced_in(99.0, 100.0, 1.0, +1, 2.0) == 0.0                                  # against the event: not "priced in"
    assert si.priced_in(None, 100.0, 1.0) is None and si.priced_in(101, 100, None) is None


PAYLOAD = {"sectors": [{"name": "Banks", "count": 10, "avg_change_pct": 1.2}, {"name": "IT", "count": 8, "avg_change_pct": 0.3},
                       {"name": "Auto", "count": 6, "avg_change_pct": -0.2}, {"name": "Pharma", "count": 5, "avg_change_pct": -0.9},
                       {"name": "Metals", "count": 5, "avg_change_pct": 0.8}, {"name": "Tiny", "count": 2, "avg_change_pct": 3.0},
                       {"name": "Other", "count": 50, "avg_change_pct": 5.0}],
           "bullish": [{"symbol": "ABC", "percentile": 96.0}, {"symbol": "MID", "percentile": 60.0}],
           "bearish": [{"symbol": "WEAK", "percentile": 90.0}]}


def test_sector_third_ignores_other_and_tiny_sectors():
    assert si.sector_third(PAYLOAD, "Banks") == "top" and si.sector_third(PAYLOAD, "Pharma") == "bottom"
    assert si.sector_third(PAYLOAD, "IT") == "mid" and si.sector_third(PAYLOAD, "Other") is None
    assert si.sector_third(PAYLOAD, "Tiny") is None and si.sector_third(None, "Banks") is None
    assert si.sector_third({"sectors": PAYLOAD["sectors"][:2]}, "Banks") is None            # fewer than 3 sectors: no opinion


def test_rs_pct():
    assert si.rs_pct(PAYLOAD, "abc") == 0.96 and si.rs_pct(PAYLOAD, "WEAK") == 0.0 and si.rs_pct(PAYLOAD, "NOPE") is None and si.rs_pct(None, "ABC") is None


def test_structure_ok():
    good = {"ms_quality": "ok", "ms_trend": "bullish"}
    assert si.structure_ok(good, None) is True
    assert si.structure_ok(good, "no price") is False and si.structure_ok(None, None) is False
    assert si.structure_ok({"ms_trend": "bearish"}, None) is False
    assert si.structure_ok({"ms_trend": "neutral", "ms_bos_dir": "bearish", "ms_bos_bars": 2, "ms_bos_failed": False}, None) is False
    assert si.structure_ok({"ms_trend": "neutral", "ms_bos_dir": "bearish", "ms_bos_bars": 2, "ms_bos_failed": True}, None) is True


# ---- S2 end to end ----------------------------------------------------------------------------------------------------------------------------------------

SUMMARY = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.5, "ms_atr": 2.0}
ORB = {"range_high": 100.0, "range_low": 99.0, "close_trigger": True, "trigger_close": 100.3}
TAPE_BUY = {"reliable": True, "buy_pct_10m": 62, "delta_agrees": True}


def s2_bars():
    rows = [bar(i, 99.6, 99.95, 99.4, 99.7) for i in range(5)]                             # opening range 09:15-09:19, high 99.95 < 100
    rows += [bar(5, 99.8, 100.4, 99.8, 100.3), bar(6, 100.3, 100.5, 100.05, 100.4)]          # break, then a dip to the range high
    rows += [bar(i, 100.4, 100.6, 100.35, 100.5) for i in range(7, 14)]
    rows += [bar(14, 100.5, 100.9, 100.45, 100.8, 3000)]                                    # last closed bar at 09:30: busy, closes high
    return rows


def run_s2(now=at(9, 31), payload=PAYLOAD, summary=SUMMARY, gap=0.4, tape=TAPE_BUY, bars=None, price=100.85, orb=ORB, symbol="ABC"):
    inputs = si.gather_long(symbol=symbol, price=price, now_ts=now.timestamp(), flow_json_orb=orb, quote={"previous_close": 100.0, "avg_price": 100.2, "open": 99.6},
                            gap_pct=gap, tape=tape, want_s2=True, want_s3=False, board_payload=payload, bars=bars if bars is not None else s2_bars(),
                            structure_summary=summary, prev_atr_pct=2.0, sector="Banks")
    return sl.classify_long(symbol=symbol, price=price, now=now.replace(tzinfo=None), event_polarity=0, flow_json=json.dumps({"orb": orb}),
                            quote={"previous_close": 100.0, "avg_price": 100.2}, gap_pct=gap, tape=tape, allowed=sl.allowed_set(s1=True, s2=True, s3=False), inputs=inputs)


def test_s2_fires_at_half_size_with_two_confirmations():
    r = run_s2()
    assert r["id"] == se.S2 and r["size_mult"] == 0.5 and r["slot"] == "0930", r
    assert "volume_above_norm" in r["confirmations"] and "tape_with_side" in r["confirmations"]


def test_s2_is_not_a_breakout_alone():
    assert run_s2(payload={**PAYLOAD, "bullish": [{"symbol": "ABC", "percentile": 70.0}]})["id"] is None      # not top decile
    assert run_s2(symbol="WEAK")["id"] is None and run_s2(symbol="NOPE")["id"] is None                         # weak side / not on the board
    assert run_s2(summary={**SUMMARY, "ms_trend": "bearish"})["id"] is None                                    # daily structure against
    assert run_s2(summary=None)["id"] is None                                                                  # structure unknown
    assert run_s2(gap=-0.5)["id"] is None and run_s2(gap=None)["id"] is None                                   # gap against / unknown
    assert run_s2(orb={**ORB, "close_trigger": False}, bars=s2_bars()[:5])["id"] is None                        # no break, no pullback hold
    assert run_s2(now=at(9, 25))["id"] is None                                                                 # S2 starts at 09:30
    assert run_s2(tape=None, bars=[b[:5] + (1000,) for b in s2_bars()])["id"] is None                          # one confirmation at most (candle only)


def test_s2_off_when_not_allowed():
    inputs = {"rs_pct": 0.96}
    r = sl.classify_long(symbol="ABC", price=100.8, now=datetime(2026, 11, 2, 9, 31), event_polarity=0, flow_json="{}", quote={}, gap_pct=0.4, allowed=sl.allowed_set(s1=True), inputs=inputs)
    assert r["id"] is None


# ---- S3 end to end ----------------------------------------------------------------------------------------------------------------------------------------

def s3_bars():
    rows = noisy(21, 100.0)                                                                # 09:15-09:35, quiet
    p = rows[-1][4]
    for k in range(1, 11):                                                                 # 09:36-09:45: a steady fall of 0.28 % a minute
        np_ = p * 0.9972
        vol = 4000 if k in (9, 10) else 1000
        rows.append(bar(20 + k, p, p + 0.02, np_ - 0.04, np_, vol))
        p = np_
    low = p
    for k in range(31, 34):                                                                 # 09:46-09:48 flat at the low
        rows.append(bar(k - 1 + 1, low + 0.02, low + 0.08, low - 0.03, low + 0.03))
    rows.append(bar(34, low + 0.02, low + 0.12, low - 0.3, low + 0.1))                      # 09:49: hammer under the earlier lows
    return rows, low


TAPE_EXH = {"reliable": True, "buy_pct_10m": 48, "delta_agrees": False, "exhaustion": "sell_exhaustion", "checks": {"imbalance": "for"}}


def run_s3(now=at(9, 50), tape=TAPE_EXH, liquid=True, gap=-0.2, vwap=None, bars=None, open_=100.0, with_wall=True):
    rows, low = s3_bars()
    price = low + 0.1
    if with_wall and isinstance(tape, dict):
        tape = {**tape, "hvn_below": price - 0.1}
    q = {"previous_close": 100.2, "avg_price": vwap if vwap is not None else price * 1.013, "open": open_}
    inputs = si.gather_long(symbol="ABC", price=price, now_ts=now.timestamp(), flow_json_orb={}, quote=q, gap_pct=gap, tape=tape, want_s2=False, want_s3=True,
                            board_payload=PAYLOAD, bars=bars if bars is not None else rows, prev_atr_pct=2.0, liquid=liquid, sector="Banks")
    res = sl.classify_long(symbol="ABC", price=price, now=now.replace(tzinfo=None), event_polarity=0, flow_json="{}", quote=q, gap_pct=gap, tape=tape,
                           allowed=sl.allowed_set(s1=False, s2=False, s3=True), inputs=inputs)
    return res, inputs


def test_s3_below_open_pullback_long_fires_at_probation_size():
    r, inputs = run_s3()
    assert inputs["z_move"] <= -2.0 and inputs["kind"] == "below_open_pullback" and inputs["failure_candle"] is True and inputs["tape_exhaustion"] is True, inputs
    assert r["id"] == se.S3 and r["size_mult"] == 0.5 and r["slot"] == "0930" and len(r["confirmations"]) >= 2, r


def test_s3_kind_follows_the_gap_and_the_open():
    assert run_s3(gap=-1.6)[1]["kind"] == "gap_down_reversal"
    assert run_s3(open_=90.0)[1]["kind"] == "panic_dump"                                  # price is above its open


def test_s3_refuses_without_each_required_part():
    assert run_s3(liquid=False)[0]["id"] is None                                          # liquid names only
    assert run_s3(tape={**TAPE_EXH, "exhaustion": None, "checks": {}})[0]["id"] is None   # no tape exhaustion
    assert run_s3(tape=None)[0]["id"] is None
    assert run_s3(now=at(9, 40))[0]["id"] is None                                         # S3 starts at 09:45
    rows, _ = s3_bars()
    assert run_s3(bars=rows[:-1] + [bar(34, rows[-1][1], rows[-1][2], rows[-1][3], rows[-1][3])])[0]["id"] is None   # last bar closes on its low: no failure candle
    assert run_s3(bars=noisy(40))[0]["id"] is None                                        # no spike at all


def test_s3_confirmations_are_measured():
    _, inputs = run_s3()
    assert inputs["vwap_stretch"] is True and inputs["wall_close"] is True and inputs["climax_volume"] is True
    _, plain = run_s3(vwap=1.0, with_wall=False)                                           # VWAP under the price: no stretch; no wall in the tape
    assert plain["vwap_stretch"] is False and plain["wall_close"] is False


# ---- S1 extras --------------------------------------------------------------------------------------------------------------------------------------------

def test_s1_gets_sector_third_and_priced_in():
    orb = {"range_high": 100.0, "close_trigger": True, "trigger_close": 100.6}
    q = {"previous_close": 100.0, "avg_price": 100.2}
    inputs = si.gather_long(symbol="ABC", price=100.8, now_ts=at(9, 30).timestamp(), flow_json_orb=orb, quote=q, gap_pct=0.5, tape=TAPE_BUY, want_s2=False, want_s3=False,
                            board_payload=PAYLOAD, prev_atr_pct=1.0, sector="Banks")
    assert inputs["sector_third"] == "top" and inputs["event_priced_in"] == 0.4
    r = sl.classify_long(symbol="ABC", price=100.8, now=datetime(2026, 11, 2, 9, 30), event_polarity=1, flow_json=json.dumps({"orb": orb}), quote=q, gap_pct=0.5,
                         tape=TAPE_BUY, inputs=inputs)
    assert r["id"] == se.S1 and "sector_top_third" in r["confirmations"]
    inputs["event_priced_in"] = 1.4                                                         # the move already ran 1.4 x the expected size
    assert sl.classify_long(symbol="ABC", price=100.8, now=datetime(2026, 11, 2, 9, 30), event_polarity=1, flow_json=json.dumps({"orb": orb}), quote=q, gap_pct=0.5,
                            tape=TAPE_BUY, inputs=inputs)["id"] is None


def test_unknown_input_keys_are_ignored():
    f = sl.facts_long(symbol="A", price=1.0, now=datetime(2026, 11, 2, 9, 30), event_polarity=0, flow_json="{}", quote={}, gap_pct=0.0, inputs={"price": 5.0, "side": "SHORT", "rs_pct": 0.9})
    assert f["side"] == "LONG" and f["price"] == 1.0 and f["rs_pct"] == 0.9


# ---- NO_SETUP refusal ---------------------------------------------------------------------------------------------------------------------------------------

def test_no_setup_refusal_rules():
    none = {"id": None, "skip": "NO_SETUP"}
    hit = {"id": "S1_CATALYST_UP"}
    assert sl.no_setup_refusal(none, mode="off", exempt=False, all_enabled=True) is None            # ships off
    assert "NO_SETUP" in sl.no_setup_refusal(none, mode="live", exempt=False, all_enabled=True)
    assert sl.no_setup_refusal(hit, mode="live", exempt=False, all_enabled=True) is None
    assert sl.no_setup_refusal(none, mode="live", exempt=True, all_enabled=True) is None            # re-arm / reclaim entry
    assert sl.no_setup_refusal(none, mode="live", exempt=False, all_enabled=False) is None          # a setup is switched off: do not block
    assert sl.no_setup_refusal({"id": None, "skip": "NO_SETUP", "why": "error"}, mode="live", exempt=False, all_enabled=True) is None   # an error is not a verdict
    assert sl.no_setup_refusal(None, mode="live", exempt=False, all_enabled=True) is None
    assert sl.no_setup_refusal(none, mode="LIVE ", exempt=False, all_enabled=True) is not None
    assert sl.no_setup_refusal(none, mode="shadow", exempt=False, all_enabled=True) is None and sl.no_setup_refusal(none, mode="", exempt=False, all_enabled=True) is None


def test_engine_refuses_s2_on_an_unknown_gap():
    f = dict(side="LONG", now=at(9, 30).replace(tzinfo=None), rs_pct=0.95, structure_ok=True, or_break=1, vol_vs_norm=1.5, candle=1, tape=0)
    assert se.classify({**f, "gap_pct": 0.3})["id"] == se.S2
    assert se.classify(f)["id"] is None


def test_wiring_in_entry_and_config():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py")).read()
    assert src.index("setup_inputs.gather_long(") < src.index("setup_live.classify_long(") < src.index("setup_live.no_setup_refusal(") < src.index("tm_gate.entry_check(candidate.symbol")
    assert "exempt=bool(rearm) or bool(_reclaim)" in src
    cfg = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py")).read()
    assert 'NO_SETUP_MODE = _get_str("NO_SETUP_MODE", "off")' in cfg and '_get_bool("SETUP_S2_ENABLED", True)' in cfg and '_get_bool("SETUP_S3_ENABLED", True)' in cfg


if __name__ == "__main__":
    ok = bad = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1
                import traceback
                print("FAIL", name, type(e).__name__, e)
                traceback.print_exc(limit=3)
    print(f"{ok} passed, {bad} failed")
    sys.exit(1 if bad else 0)
