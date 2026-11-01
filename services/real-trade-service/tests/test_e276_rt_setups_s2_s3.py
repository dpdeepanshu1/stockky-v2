"""E276 round 68: Real Trade S2_RS_BREAKOUT and S3_EXHAUSTION (long). Plain runner: python3 tests/test_e276_rt_setups_s2_s3.py (service folder).
Stdlib only; the clock, bars, Market Board body and structure summary are made up. Runs the real chain entry.py runs: orb_bars.range_trigger -> setup_inputs.gather_long
-> setup_live.classify_long -> setup_engine.classify."""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import orb_bars as OB  # noqa: E402
from entry_engine import setup_engine as se  # noqa: E402
from entry_engine import setup_inputs as si  # noqa: E402
from entry_engine import setup_live as SL  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()


def at(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=IST)


def bar(minute, o, h, l, c, v=1000):
    return (DAY + minute * 60, o, h, l, c, v)                     # minute 0 = 09:15


def noisy(n, base=100.0, seed=1):
    rnd = random.Random(seed)
    out, p = [], base
    for i in range(n):
        o = p
        p = base + rnd.uniform(-0.06, 0.06)
        out.append(bar(i, o, max(o, p) + 0.02, min(o, p) - 0.02, p))
    return out


PAYLOAD = {"sectors": [{"name": "Banks", "count": 10, "avg_change_pct": 1.2}, {"name": "IT", "count": 8, "avg_change_pct": 0.3},
                       {"name": "Auto", "count": 6, "avg_change_pct": -0.2}, {"name": "Pharma", "count": 5, "avg_change_pct": -0.9},
                       {"name": "Metals", "count": 5, "avg_change_pct": 0.8}],
           "bullish": [{"symbol": "ABC", "percentile": 96.0}, {"symbol": "MID", "percentile": 60.0}],
           "bearish": [{"symbol": "WEAK", "percentile": 90.0}]}
SUMMARY = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.5, "ms_atr": 2.0}
BIG = 500.0 * 1e7            # Rs 500 crore normal daily turnover


# ---- S2 ---------------------------------------------------------------------------------------------------------------------------------------------------

def s2_bars():
    rows = [bar(i, 99.6, 99.95, 99.4, 99.7) for i in range(5)]                             # opening range 09:15-09:19, high 99.95
    rows += [bar(5, 99.8, 100.4, 99.8, 100.3), bar(6, 100.3, 100.5, 100.05, 100.4)]          # a closed break, then a dip back to the range high
    rows += [bar(i, 100.4, 100.6, 100.35, 100.5) for i in range(7, 14)]
    rows += [bar(14, 100.5, 100.9, 100.45, 100.8, 3000)]                                    # 09:29 bar: busy, closes high
    return rows


def run_s2(now=at(9, 31), payload=PAYLOAD, summary=SUMMARY, reason=None, gap=0.4, imb=0.3, bars=None, price=100.85, symbol="ABC", allowed=None):
    rows = bars if bars is not None else s2_bars()
    orb = OB.range_trigger(rows, now)
    inputs = si.gather_long(symbol=symbol, price=price, now_ts=now.timestamp(), bars=rows, board_payload=payload, structure_summary=summary, structure_reason=reason,
                            prev_close=100.0, atr_pct=2.0, gap_pct=gap, day_open=99.6, vwap=orb.get("vwap"), orb=orb, tape=None, imbalance_5=imb, avg_turnover=BIG,
                            sector="Banks", liquid_min_cr=100.0, want_s2=True, want_s3=False)
    res = SL.classify_long(price=price, prev_close=100.0, gap_pct=gap, event=None, bars_facts=orb, imbalance_5=imb, now=now, inputs=inputs,
                           allowed=allowed or SL.allowed_set(s1=True, s2=True, s3=False))
    return res, inputs


def test_s2_fires_at_half_size_with_two_confirmations():
    r, inputs = run_s2()
    assert inputs["rs_pct"] == 0.96 and inputs["structure_ok"] is True and inputs["or_break"] == 1 and inputs["gap_pct"] == 0.4, inputs
    assert r["id"] == se.S2 and r["size_mult"] == 0.5 and r["slot"] == "0930", r
    assert "volume_above_norm" in r["confirmations"] and "tape_with_side" in r["confirmations"], r


def test_s2_is_not_a_breakout_alone():
    assert run_s2(payload={**PAYLOAD, "bullish": [{"symbol": "ABC", "percentile": 70.0}]})[0]["id"] is None      # not top decile
    assert run_s2(symbol="WEAK")[0]["id"] is None and run_s2(symbol="NOPE")[0]["id"] is None                     # weak side / not on the board
    assert run_s2(payload={})[0]["id"] is None and run_s2(payload=None)[0]["id"] is None                          # board down
    assert run_s2(summary={**SUMMARY, "ms_trend": "bearish"})[0]["id"] is None                                    # daily structure against
    assert run_s2(summary=None)[0]["id"] is None and run_s2(reason="stale")[0]["id"] is None                       # structure unknown / unusable
    assert run_s2(gap=-0.5)[0]["id"] is None and run_s2(gap=None)[0]["id"] is None                                 # gap against / unknown
    assert run_s2(bars=s2_bars()[:5])[0]["id"] is None                                                             # no break, no pullback hold
    assert run_s2(now=at(9, 25))[0]["id"] is None                                                                  # S2 starts at 09:30
    assert run_s2(imb=None, bars=[b[:5] + (1000,) for b in s2_bars()])[0]["id"] is None                            # one confirmation at most (candle only)
    assert run_s2(allowed=SL.allowed_set(s1=True))[0]["id"] is None                                                # S2 switched off


def test_s2_needs_the_live_price_still_over_the_range_high():
    r, inputs = run_s2(price=99.0)
    assert inputs["or_break"] == 0 and inputs["pullback_hold"] is False and r["id"] is None      # the closed break is real, but the live price fell back under the range high


# ---- S3 ---------------------------------------------------------------------------------------------------------------------------------------------------

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
        rows.append(bar(k, low + 0.02, low + 0.08, low - 0.03, low + 0.03))
    rows.append(bar(34, low + 0.02, low + 0.12, low - 0.3, low + 0.1))                      # 09:49: hammer under the earlier lows
    return rows, low


TAPE_EXH = {"reliable": True, "buy_pct_10m": 48, "delta_agrees": False, "exhaustion": "sell_exhaustion", "checks": {"imbalance": "for"}}


def run_s3(now=at(9, 50), tape=TAPE_EXH, imb=0.0, turnover=BIG, gap=-0.2, vwap=None, bars=None, day_open=100.0, allowed=None):
    rows, low = s3_bars()
    rows = bars if bars is not None else rows
    price = low + 0.1
    orb = OB.range_trigger(rows, now)
    inputs = si.gather_long(symbol="ABC", price=price, now_ts=now.timestamp(), bars=rows, board_payload=PAYLOAD, structure_summary=None, structure_reason=None,
                            prev_close=100.2, atr_pct=2.0, gap_pct=gap, day_open=day_open, vwap=vwap if vwap is not None else price * 1.013, orb=orb, tape=tape,
                            imbalance_5=imb, avg_turnover=turnover, sector="Banks", liquid_min_cr=100.0, want_s2=False, want_s3=True)
    res = SL.classify_long(price=price, prev_close=100.2, gap_pct=gap, event=None, bars_facts=orb, imbalance_5=imb, now=now, inputs=inputs,
                           allowed=allowed or SL.allowed_set(s1=False, s2=False, s3=True))
    return res, inputs


def test_s3_below_open_pullback_fires_at_probation_size():
    r, inputs = run_s3()
    assert inputs["z_move"] <= -2.0 and inputs["kind"] == "below_open_pullback" and inputs["failure_candle"] is True and inputs["tape_exhaustion"] is True \
        and inputs["liquid"] is True, inputs
    assert r["id"] == se.S3 and r["size_mult"] == 0.5 and r["slot"] == "0930" and len(r["confirmations"]) >= 2, r


def test_s3_book_lean_stands_in_for_the_tape():
    r, inputs = run_s3(tape=None, imb=0.4)
    assert inputs["tape_exhaustion"] is True and r["id"] == se.S3, (r, inputs)          # no flow tape: a bid-heavy 5-level book counts
    r, inputs = run_s3(tape=None, imb=0.0)
    assert inputs["tape_exhaustion"] is False and r["id"] is None


def test_s3_kind_follows_the_gap_and_the_open():
    assert run_s3(gap=-1.6)[1]["kind"] == "gap_down_reversal"
    assert run_s3(day_open=90.0)[1]["kind"] == "panic_dump"


def test_s3_refuses_without_each_required_part():
    assert run_s3(turnover=5.0 * 1e7)[0]["id"] is None and run_s3(turnover=None)[0]["id"] is None   # liquid names only; unknown turnover is not liquid
    assert run_s3(now=at(9, 40))[0]["id"] is None                                                   # S3 starts at 09:45
    rows, _ = s3_bars()
    assert run_s3(bars=rows[:-1] + [bar(34, rows[-1][1], rows[-1][2], rows[-1][3], rows[-1][3])])[0]["id"] is None   # last bar closes on its low: no failure candle
    assert run_s3(bars=noisy(40))[0]["id"] is None                                                  # no spike at all
    assert run_s3(allowed=SL.allowed_set(s1=True))[0]["id"] is None                                 # S3 switched off


def test_s3_confirmations_are_measured():
    _, inputs = run_s3()
    assert inputs["vwap_stretch"] is True and inputs["climax_volume"] is True and inputs["wall_close"] is False    # no support node in the RT tape
    assert run_s3(vwap=1.0)[1]["vwap_stretch"] is False


# ---- shared behaviour ---------------------------------------------------------------------------------------------------------------------------------------

def test_missing_facts_are_never_true():
    f = si.gather_long(symbol="ABC", price=None, now_ts=at(9, 50).timestamp(), bars=None, board_payload=None, structure_summary=None, structure_reason=None,
                       prev_close=None, atr_pct=None, gap_pct=None, day_open=None, vwap=None, orb=None, tape=None, imbalance_5=None, avg_turnover=None)
    assert f.get("structure_ok") is False and "rs_pct" not in f and "gap_pct" not in f and "z_move" not in f and f.get("liquid") is not True, f
    r = SL.classify_long(price=None, prev_close=None, gap_pct=None, event=None, bars_facts=None, imbalance_5=None, now=at(9, 50), inputs=f,
                         allowed=SL.allowed_set(s1=True, s2=True, s3=True))
    assert r["id"] is None


def test_s1_gets_sector_third_and_priced_in():
    f = si.gather_long(symbol="ABC", price=101.0, now_ts=at(9, 31).timestamp(), bars=None, board_payload=PAYLOAD, structure_summary=None, structure_reason=None,
                       prev_close=100.0, atr_pct=1.0, gap_pct=0.5, day_open=None, vwap=None, orb=None, tape=None, imbalance_5=None, avg_turnover=None,
                       sector="Banks", want_s2=False, want_s3=False)
    assert f["sector_third"] == "top" and f["event_priced_in"] == 0.5, f
    assert si.sector_third(PAYLOAD, "Other") is None
    assert si.liquid_by_turnover(BIG, 100.0) is True and si.liquid_by_turnover(1.0e7, 100.0) is False and si.liquid_by_turnover(BIG, 0) is False


def test_unknown_input_keys_are_ignored():
    f = SL.facts_long(price=1.0, prev_close=1.0, gap_pct=0.0, event=None, bars_facts=None, imbalance_5=None, now=at(9, 30),
                      inputs={"price": 5.0, "side": "SHORT", "service": "PS", "rs_pct": 0.9})
    assert f["side"] == "LONG" and f["service"] == "RT" and f["price"] == 1.0 and f["rs_pct"] == 0.9


def test_s1_alone_without_inputs_as_before():
    f = SL.facts_long(price=1.0, prev_close=1.0, gap_pct=0.0, event=None, bars_facts=None, imbalance_5=None, now=at(9, 30))
    assert f["allowed"] == {se.S1}


def test_board_client_keeps_the_payload():
    try:
        from entry_engine import board_client
    except ModuleNotFoundError as e:                       # httpx is not installed in the bare runner; the VM suite has it
        print("skip board_client:", e)
        return
    board_client.reset()
    assert board_client._cache["payload"] == {}
    board_client._cache.update(at=float("inf"), rows={"ABC": {}}, payload=PAYLOAD)          # a fresh cache entry: no network call
    assert asyncio.run(board_client.get_payload()) is PAYLOAD
    board_client.reset()
    assert board_client._cache["payload"] == {}


def test_range_end_for():
    assert si.range_end_for(at(9, 31).timestamp()) == at(9, 20).timestamp()
    assert si.day_open_ts(at(14, 0).timestamp()) == at(9, 15).timestamp()


def test_no_setup_refusal_rules():
    none = {"id": None, "skip": "NO_SETUP"}
    kw = dict(exempt=False, all_ran=True)
    assert "NO_SETUP" in SL.no_setup_refusal(none, mode="live", **kw)
    assert SL.no_setup_refusal(none, mode="off", **kw) is None and SL.no_setup_refusal(none, mode=None, **kw) is None       # ships off
    assert SL.no_setup_refusal(none, mode="LIVE ", **kw) is not None                                                         # case / spaces forgiven
    assert SL.no_setup_refusal(none, mode="live", exempt=True, all_ran=True) is None                                         # pullback re-arm carries its own trigger
    assert SL.no_setup_refusal(none, mode="live", exempt=False, all_ran=False) is None                                       # before 09:45 / a setup switched off
    assert SL.no_setup_refusal(None, mode="live", **kw) is None                                                              # the walk did not run: no verdict
    assert SL.no_setup_refusal({"id": None, "skip": "NO_SETUP", "why": "error"}, mode="live", **kw) is None                  # an engine error is not a verdict
    assert SL.no_setup_refusal({"id": None, "skip": "PENALTY_TOO_HIGH"}, mode="live", **kw) is None
    assert SL.no_setup_refusal({"id": se.S2, "skip": None}, mode="live", **kw) is None                                       # a match is never refused
    r, _ = run_s2()
    assert SL.no_setup_refusal(r, mode="live", **kw) is None
    r, _ = run_s2(symbol="NOPE")                                                                                             # real engine verdict for a stock with no setup
    assert "NO_SETUP" in SL.no_setup_refusal(r, mode="live", **kw)
    assert config.NO_SETUP_MODE == "off"


def blk_after(src):
    i = src.index("# E276 round 70: \"no setup, no trade\"")
    return src[i:i + 1200]


def test_wiring_and_switches():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    i = src.index("# E273 round 19 / E276 round 68")
    blk = src[i:src.index("# E273 round 20: a gap-up refusal")]
    assert i < src.index("max_trade_risk = account_state.equity") and "except Exception" in blk and "SETUP_S2_ENABLED" in blk and "SETUP_S3_ENABLED" in blk
    assert "_setup_inputs.gather_long" in blk and "allowed_set" in blk and "_board_client.get_payload" in blk
    assert config.SETUP_S2_ENABLED is True and config.SETUP_S3_ENABLED is True and config.SETUP_S3_LIQUID_MIN_TURNOVER_CR == 100.0
    cat = (HERE / "control" / "catalog.py").read_text()
    assert 'Flag("SETUP_S2_ENABLED"' in cat and 'Flag("SETUP_S3_ENABLED"' in cat and 'Flag("NO_SETUP_MODE"' in cat
    assert "no_setup_refusal" in blk_after(src) and "_ns_all_ran = bool(_s1_on and _s2_on and _s3_on)" in src


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
