"""Plan correctness cases through analyze(): T-ST (wick-only break), T-CA (split), T-FVG, T-LQ, T-OB, T-NLA (no lookahead).
Crafted bars: a quiet sine oscillation followed by one or two hand-built bars. Runs with pytest or plain `python`."""
import math, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure.engine import analyze

T0 = 1_700_000_000


def mk(i, o, h, l, c, v=1000.0):
    return (T0 + i * 86400, o, h, l, c, v)


def osc(n, base=100.0, amp=3.0, per=16):
    out, prev = [], base
    for k in range(n):
        c = base + amp * math.sin(2 * math.pi * k / per)
        out.append(mk(k, prev, max(prev, c) + 0.4, min(prev, c) - 0.4, c))
        prev = c
    return out


def test_wick_only_break_is_not_bos_but_a_close_is():
    b = osc(140)
    hi, last, n = max(x[2] for x in b[-20:]), b[-1][4], len(b)
    wick = analyze(b + [mk(n, last, hi + 1.0, last - 0.3, last + 0.1)])
    assert wick["bos"]["direction"] is None
    close = analyze(b + [mk(n, last, hi + 2.5, last - 0.1, hi + 2.0)])
    assert close["bos"]["direction"] == "bullish" and close["bos"]["level"] == round(hi, 2) and close["bos"]["failed"] is False


def test_split_creates_no_fake_zone_or_break():
    base = osc(180)
    sp = [(x[0], x[1] / 2, x[2] / 2, x[3] / 2, x[4] / 2, x[5] * 2) if i >= 70 else x for i, x in enumerate(base)]
    r = analyze(sp)
    assert r["data_quality"]["ca_discontinuity"] is True and r["data_quality"]["bars_used"] == 110
    assert r["bos"]["direction"] is None and r["choch"]["direction"] is None
    for z in (r["fvg"], r["order_block"]):  # nothing may be built across the pre-split prices (~100)
        assert z is None or z["top"] < 60
    assert all(s["price"] < 60 for s in r["swings"]) and r["atr"] < 5


def test_bullish_fvg_is_found_with_the_right_edges():
    g = osc(140)
    n, last = len(g), g[-1][4]
    g += [mk(n, last, last + 0.3, last - 0.3, last), mk(n + 1, last, last + 4.0, last - 0.1, last + 3.8), mk(n + 2, last + 3.9, last + 4.6, last + 1.6, last + 4.4)]
    f = analyze(g)["fvg"]
    assert f and f["side"] == "bullish" and f["status"] == "fresh"
    assert f["bottom"] == round(last + 0.3, 2) and f["top"] == round(last + 1.6, 2)


def test_sell_side_sweep_then_reclaim():
    s = osc(140)
    lo, last, n = min(x[3] for x in s[-30:]), s[-1][4], len(s)
    s += [mk(n, last, last + 0.2, lo - 0.8, lo + 0.6), mk(n + 1, lo + 0.6, lo + 1.5, lo + 0.3, lo + 1.2)]
    sw = analyze(s)["liquidity"]["sweep"]
    assert sw["direction"] == "bullish" and sw["level"] == round(lo, 2) and sw["bars_since"] == 1


def test_demand_order_block_is_the_last_down_candle_before_displacement():
    o = osc(140)
    n, last = len(o), o[-1][4]
    o += [mk(n, last, last + 0.2, last - 1.0, last - 0.8), mk(n + 1, last - 0.8, last + 5.0, last - 0.9, last + 4.8)]
    z = analyze(o)["order_block"]
    assert z and z["side"] == "bullish" and z["status"] == "fresh"
    assert z["top"] == round(last, 2) and z["bottom"] == round(last - 0.8, 2) and z["impulse"] >= 1.5


def test_nla_events_found_on_truncated_history_survive_on_full_history():
    """T-NLA: every swing / BOS / CHOCH seen on bars[:k] is unchanged on the full series (future bars never rewrite the past)."""
    from market_structure.atr import atr_series
    from market_structure.params import MSParams
    from market_structure.prep import prep
    from market_structure.structure import scan_structure
    from market_structure.swings import raw_swings
    random.seed(11)
    px, bars = 100.0, []
    for i in range(300):
        o, c = px, max(5.0, px + random.gauss(0.2 if (i // 35) % 2 == 0 else -0.2, 1.6))
        bars.append(mk(i, o, max(o, c) + abs(random.gauss(0, 0.8)), min(o, c) - abs(random.gauss(0, 0.8)), c))
        px = c

    def run(bs):
        p = MSParams()
        pr = prep(bs, p)
        b = pr["bars"]
        a = atr_series(b, p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)
        raw = raw_swings(b, a, p)
        return raw, scan_structure(b, a, raw, pr["gap_event"], pr["circuit"], p)

    full_raw, full = run(bars)
    key = lambda e: (e["break_idx"], e["direction"], round(e["level"], 6))
    seen = 0
    for k in (120, 160, 200, 240, 280):
        raw, st = run(bars[:k])
        fr = {(s["idx"], s["kind"], s["price"]) for s in full_raw if s["confirmed_at"] < k}
        assert {(s["idx"], s["kind"], s["price"]) for s in raw} == fr
        for name in ("bos", "choch"):
            fk = {key(e) for e in full[name]}
            assert all(key(e) in fk for e in st[name]), (name, k)
            seen += len(st[name])
    assert seen > 0, "fixture produced no BOS/CHOCH, the test would prove nothing"


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
