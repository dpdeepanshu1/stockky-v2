"""Tests for scripts/sweep_study.py (E278). Plain asserts: python3 scripts/test_sweep_study.py  or  python3 -m pytest scripts/test_sweep_study.py -q

Fixed bars only: no network, no clock, no live service. The study is offline, so these tests pin its definitions (what is a sweep, what is a failed break),
its statistics (day-clustered bootstrap, both intervals above zero, fewer than 30 events = counts only) and its direction-agnostic verdicts.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import _intraday_data as idata  # noqa: E402
import sweep_study as ss  # noqa: E402
from market_structure.atr import atr_series  # noqa: E402

DAY = 86400.0
T0 = 1609459200.0                      # 2021-01-01 00:00 UTC


def _flat(n, px=100.0, rng=2.0):
    """n daily bars (t, o, h, l, c, v), flat, each with a +-rng/2 range."""
    return [[T0 + k * DAY, px, px + rng / 2, px - rng / 2, px, 1000.0] for k in range(n)]


# ----------------------------------------------------------------------------------------------------------------- definitions (part A)
def test_classify_day_sweep_needs_a_close_back_inside():
    bars = _flat(30)
    atr = atr_series(bars, 14)
    i = 20
    bars[i] = [bars[i][0], 100.0, 100.8, 98.0, 100.2, 1000.0]          # low 99 -> 98: pierces by 1.0 (0.5 ATR) and closes above the old low 99
    assert ss.classify_day(bars, atr, i, 0.10) == "sweep_low"
    bars[i] = [bars[i][0], 100.0, 100.8, 98.0, 98.6, 1000.0]           # closes BELOW the old low: a breakdown, not a sweep
    assert ss.classify_day(bars, atr, i, 0.10) == "break_low"
    bars[i] = [bars[i][0], 100.0, 102.0, 99.2, 100.2, 1000.0]          # high 101 -> 102, closes below it
    assert ss.classify_day(bars, atr, i, 0.10) == "sweep_high"
    bars[i] = [bars[i][0], 100.0, 102.0, 99.2, 101.6, 1000.0]
    assert ss.classify_day(bars, atr, i, 0.10) == "break_high"


def test_classify_day_depth_outside_bar_and_no_atr():
    bars = _flat(30)
    atr = atr_series(bars, 14)
    i = 20
    bars[i] = [bars[i][0], 100.0, 100.8, 98.95, 100.2, 1000.0]         # pierces 99 by only 0.05 (0.025 ATR): below the 0.10 ATR depth
    assert ss.classify_day(bars, atr, i, 0.10) is None
    assert ss.classify_day(bars, atr, i, 0.01) == "sweep_low"          # a shallower pre-set depth would call it a sweep
    bars[i] = [bars[i][0], 100.0, 102.5, 97.5, 100.2, 1000.0]          # both sides pierced
    assert ss.classify_day(bars, atr, i, 0.10) == "outside"
    assert ss.classify_day(bars, atr, 0, 0.10) is None                 # the first day has no previous bar
    short = _flat(3)
    assert ss.classify_day(short, atr_series(short, 14), 2, 0.10) is None   # no ATR yet


def test_classify_day_skips_split_artefact():
    bars = _flat(30)
    atr = atr_series(bars, 14)
    bars[20] = [bars[20][0], 60.0, 61.0, 40.0, 60.0, 1000.0]           # close -40 % in a day: a bonus / split, not a sweep
    assert ss.classify_day(bars, atr, 20, 0.10) is None


def test_gross_ret_enters_next_open_exits_close_of_h():
    bars = _flat(10)
    bars[5] = [bars[5][0], 100.0, 101.0, 99.0, 100.0, 1.0]
    bars[6] = [bars[6][0], 102.0, 104.0, 101.0, 103.0, 1.0]
    bars[7] = [bars[7][0], 103.0, 106.0, 102.0, 105.0, 1.0]
    assert abs(ss.gross_ret(bars, 5, 1) - (103.0 / 102.0 - 1) * 100) < 1e-9      # next session open -> close
    assert abs(ss.gross_ret(bars, 5, 2) - (105.0 / 102.0 - 1) * 100) < 1e-9
    assert ss.gross_ret(bars, 9, 1) is None and ss.gross_ret(bars, 8, 3) is None  # not enough bars
    bars[6] = [bars[6][0], 130.0, 131.0, 129.0, 130.0, 1.0]
    assert ss.gross_ret(bars, 5, 1) is None                                        # an absurd gap (> 20 %) is dropped, not scored


# ----------------------------------------------------------------------------------------------------------------- statistics
def test_boot_needs_clusters_and_is_deterministic():
    few = [[1, 1.0, 1, 0.0]] * 5
    assert ss.boot(few, 200)["ci_net"] is None
    cl = [[2, 2.0 + (k % 3) * 0.1, 2, 0.0] for k in range(40)]
    a, b = ss.boot(cl, 200, seed=3), ss.boot(cl, 200, seed=3)
    assert a == b and a["ci_net"][0] > 0.9 and a["ci_excess"][0] > 0.9 and a["clusters"] == 40


def test_side_ok_needs_both_intervals_above_zero():
    assert ss.side_ok({"ci_net": [0.1, 1.0], "ci_excess": [0.2, 1.1]})
    assert not ss.side_ok({"ci_net": [-0.1, 1.0], "ci_excess": [0.2, 1.1]})      # not positive after costs
    assert not ss.side_ok({"ci_net": [0.1, 1.0], "ci_excess": [-0.2, 1.1]})      # does not beat the baseline
    assert not ss.side_ok({"ci_net": None, "ci_excess": None})


def test_verdict_labels():
    good = {"ci_net": [0.2, 1.0], "ci_excess": [0.3, 1.0]}
    bad = {"ci_net": [-1.0, -0.2], "ci_excess": [-1.0, -0.3]}
    flat = {"ci_net": [-0.5, 0.5], "ci_excess": [-0.5, 0.5]}
    assert ss.verdict(100, good, bad) == "reversal"
    assert ss.verdict(100, bad, good) == "continuation"
    assert ss.verdict(100, flat, flat) == "neither"
    assert ss.verdict(100, bad, bad) == "neither"
    assert ss.verdict(29, good, bad) == "insufficient_sample"                      # fewer than 30 events: nothing else
    assert ss.verdict(100, {"ci_net": None}, bad) == "insufficient_sample"         # no interval, no verdict


# ----------------------------------------------------------------------------------------------------------------- part A end to end
def _history(seed, n, event_every, next_day_pct, noise=0.4, kind="sweep_low"):
    """A random-walk daily history with an engineered event every `event_every` days and a known move on the next session (open -> close)."""
    rng = random.Random(seed)
    bars = []
    px = 100.0
    for k in range(n):
        o = px
        c = o * (1 + rng.uniform(-noise, noise) / 100.0)
        bars.append([T0 + k * DAY, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000.0])
        px = c
    first = 20 + (seed % event_every)
    for i in range(first, n - 3, event_every):
        pb = bars[i - 1]
        rng_prev = pb[2] - pb[3]
        o = pb[4]
        if kind == "sweep_low":
            low = pb[3] - rng_prev                                    # pierces the previous low by a whole previous range
            c = pb[3] + 0.2 * rng_prev                                # and closes back above it
            bars[i] = [bars[i][0], o, min(pb[2], max(o, c) * 1.002), low, c, 1000.0]
        # the known next session: open = this close, close = open * (1 + next_day_pct)
        j = i + 1
        o2 = bars[i][4]
        c2 = o2 * (1 + next_day_pct / 100.0)
        bars[j] = [bars[j][0], o2, max(o2, c2) * 1.002, min(o2, c2) * 0.998, c2, 1000.0]
        if j + 1 < n:                                                 # keep the walk continuous
            bars[j + 1][1] = c2
            bars[j + 1][2] = max(bars[j + 1][2], c2)
            bars[j + 1][3] = min(bars[j + 1][3], c2)
    return bars


def _study(next_day_pct, kind="sweep_low", symbols=4, n=500, min_events=30):
    rows = []
    for s in range(symbols):
        bars = _history(s + 1, n, 11 + s, next_day_pct, kind=kind)
        r = ss.stock_events(bars, f"S{s}", 0.10, (1,), None)
        rows += r["events"]
    return rows


def test_stock_events_baseline_excludes_events_and_needs_five_days():
    bars = _history(1, 400, 12, 3.0)
    r = ss.stock_events(bars, "X", 0.10, (1,), None)
    ev = [e for e in r["events"] if e["kind"] == "sweep_low"]
    assert len(ev) >= 25, len(ev)
    assert all(e["b1"] is not None and abs(e["b1"]) < 1.5 for e in ev)             # the baseline is ordinary days, not the +3 % follow-through days
    planted = [e for e in ev if abs(e["g1"] - 3.0) < 0.01]                         # the engineered +3 % next session (open -> close); natural sweeps add noise events
    assert len(planted) >= 25, [round(e["g1"], 2) for e in ev][:8]
    thin = _flat(40)
    thin[30] = [thin[30][0], 100.0, 100.8, 98.0, 100.2, 1000.0]
    r2 = ss.stock_events(thin, "Y", 0.10, (1,), None)
    assert len(r2["events"]) <= 1                                                  # a single flat month: the baseline pool is the other flat days of the month


def test_part_a_finds_reversal_when_the_next_session_pays():
    rows = _study(3.0)
    t = ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, False, 30)
    assert t["n"] >= 30 and t["verdict"] == "reversal", t
    assert t["reversal_side"] == "long" and t["long"]["mean_net"] > 0.3 and t["short"]["mean_net"] < -1.0


def test_part_a_finds_continuation_when_the_next_session_falls():
    rows = _study(-3.0)
    t = ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, False, 30)
    assert t["verdict"] == "continuation", t
    assert t["continuation_side"] == "short" and t["short"]["mean_net"] > 0.3


def test_part_a_says_neither_when_nothing_happens():
    rows = _study(0.0)
    t = ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, False, 30)
    assert t["verdict"] == "neither", t                                           # an event that moves nothing cannot pay a 0.40 % round trip


def test_part_a_fewer_than_30_events_prints_counts_only():
    rows = [r for r in _study(3.0, symbols=1, n=200) if r["kind"] == "sweep_low"][:20]     # a random walk sweeps often by itself: cap at 20 events
    t = ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, False, 30)
    assert t["n"] == 20 and t["verdict"] == "insufficient_sample"
    assert set(t) == {"kind", "horizon", "n", "days", "verdict"}                   # nothing else: no means, no intervals


def test_part_a_market_adjusted_needs_the_benchmark_return():
    rows = [dict(r) for r in _study(3.0)]
    for r in rows:
        r["m1"] = 3.0                                                              # the whole +3 % was the market
    t = ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, True, 30)
    assert t["verdict"] in ("neither", "continuation") and t["long"]["mean_net"] < 0.5, t
    for r in rows:
        r["m1"] = None
    assert ss.summarize_a(rows, "sweep_low", 1, 0.40, 200, True, 30)["verdict"] == "insufficient_sample"


def test_part_a_cli_writes_reports():
    with tempfile.TemporaryDirectory() as td:
        for s in range(4):
            bars = _history(s + 1, 500, 11 + s, 3.0)
            cs = [{"date": float(b[0]), "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": 1000} for b in bars]
            (Path(td) / f"SYM{s}.json").write_text(json.dumps({"candles": cs}))
        out = Path(td) / "o"
        rc = ss.main(["--daily", td, "--out", str(out), "--parts", "A", "--boot", "100", "--horizons", "1", "--depths", "0.10,0.20"])
        rep = json.loads((out / "sweep_study.json").read_text())
        assert rc == 0 and (out / "sweep_study.md").exists()
        assert rep["variants_tried"] == 2 and [v["primary"] for v in rep["A"]["variants"]] == [True, False]
        prim = {t["kind"]: t["verdict"] for t in rep["A"]["variants"][0]["tables"] if not t["market_adjusted"]}
        assert prim["sweep_low"] == "reversal", prim
    assert ss.main(["--parts", "A"]) == 1                                          # nothing to read: non-zero, no crash
    assert ss.main(["--depths", "0"]) == 2


# ----------------------------------------------------------------------------------------------------------------- part C
def _mb(closes, hi=101.0, lo=100.0):
    """A minute map {offset: (t, o, h, l, c, v)}: five range bars (09:15-09:19) then one bar per close."""
    mb = {k: (0.0, 100.5, hi if k == 2 else 100.8, lo if k == 3 else 100.2, 100.5, 1.0) for k in range(5)}
    for n, c in enumerate(closes):
        mb[5 + n] = (0.0, c, c + 0.1, c - 0.1, c, 1.0)
    return mb


def test_opening_break_failed_and_held():
    failed = ss.opening_break(_mb([101.5, 100.8, 100.8, 100.8]))
    assert failed == {"kind": "failed", "side": 1, "break_off": 5, "entry_off": 7}
    held = ss.opening_break(_mb([101.5, 101.8, 102.0, 102.1, 102.2]))
    assert held == {"kind": "held", "side": 1, "break_off": 5, "entry_off": 9}
    down = ss.opening_break(_mb([99.5, 99.4, 100.5]))                              # a down break that closes back inside on bar 7 (the 2nd bar after it)
    assert down == {"kind": "failed", "side": -1, "break_off": 5, "entry_off": 8}
    assert ss.opening_break(_mb([100.5] * 40)) is None                              # never breaks
    mb = _mb([100.5] * 36 + [101.5, 101.6, 101.7, 101.8, 101.9])                    # first break at offset 41 (> 09:55): not studied
    assert ss.opening_break(mb) is None
    mb = _mb([101.5, 100.8])
    del mb[2]
    assert ss.opening_break(mb) is None                                             # a hole in the opening range


def test_opening_break_a_close_after_three_bars_is_still_held():
    r = ss.opening_break(_mb([101.5, 101.6, 101.7, 101.8, 100.8]))                 # back inside only on the 4th bar after the break
    assert r["kind"] == "held"


def _write_day(root, date, sym, bars):
    d = Path(root) / date
    d.mkdir(parents=True, exist_ok=True)
    (d / (quote(sym, safe="") + ".json")).write_text(json.dumps({"v": 1, "symbol": sym, "date": date, "tf": 1, "first_ts": bars[0][0], "last_ts": bars[-1][0],
                                                                  "n": len(bars), "backfilled": False, "bars": bars}))


def _session(date, fn):
    """375 one-minute bars; fn(off) -> (o, h, l, c)."""
    t0 = idata.open_ts(date)
    out = []
    for off in range(375):
        o, h, l, c = fn(off)
        out.append([t0 + 60 * off, o, h, l, c, 1000.0])
    return out


def _or_day(date, kind, drift):
    """A session with a 100-101 opening range, an up-break at 09:20 and a known drift per minute after the entry."""
    def fn(off):
        if off < 5:
            return (100.5, 101.0 if off == 2 else 100.8, 100.0 if off == 3 else 100.2, 100.5)
        if off == 5:
            return (100.9, 101.6, 100.9, 101.5)                      # the closed break
        if kind == "failed" and off in (6, 7, 8):
            return (100.9, 101.0, 100.5, 100.8)                      # back inside
        base = 100.8 if kind == "failed" else 101.5
        px = base * (1 + drift * (off - 8) / 100.0) if off > 8 else base
        return (px, px * 1.0005, px * 0.9995, px)
    return _session(date, fn)


def test_part_c_end_to_end_reversal():
    dates = [f"2026-10-{d:02d}" for d in range(1, 29) if d % 7 not in (6, 0)][:20]
    with tempfile.TemporaryDirectory() as td:
        for k, d in enumerate(dates):
            for s in range(4):
                # failed breaks keep FALLING after the reclaim (a fade pays); held breaks drift sideways
                _write_day(td, d, f"F{s}", _or_day(d, "failed", -0.02))
                _write_day(td, d, f"H{s}", _or_day(d, "held", 0.0))
        rep = ss.run_part_c(Path(td), 0.15, 200, 30)
        prim = next(t for t in rep["tables"] if t["horizon"] == "10:00")
        assert rep["failed"] == 80 and rep["held"] == 80, rep
        assert prim["failed_n"] == 80 and prim["held_n"] == 80
        assert prim["verdict"].startswith("reversal"), prim
        assert prim["fade"]["mean_net_failed"] > 0.15 and prim["follow"]["mean_net_failed"] < 0


def test_part_c_few_events_is_insufficient():
    with tempfile.TemporaryDirectory() as td:
        for d in ("2026-10-01", "2026-10-02"):
            _write_day(td, d, "A", _or_day(d, "failed", -0.02))
        rep = ss.run_part_c(Path(td), 0.15, 100, 30)
        assert all(t["verdict"] == "insufficient_sample" for t in rep["tables"]) and "fade" not in rep["tables"][0]


# ----------------------------------------------------------------------------------------------------------------- part B
def _expand(cands5):
    """5-minute candles (o, h, l, c) -> one-minute bars [t-offset-free]: five bars per candle that start at o, end at c and contain h / l."""
    out = []
    for o, h, l, c in cands5:
        pts = [o, h, l, (h + l) / 2.0, c]
        prev = o
        for k, p in enumerate(pts):
            bo = prev if k else o
            out.append((bo, max(bo, p, h if k == 1 else p), min(bo, p, l if k == 2 else p), p))
            prev = p
    return out


def _day_from_5m(date, cands5):
    fill = cands5[-1][3]
    cands5 = list(cands5) + [(fill, fill, fill, fill)] * (75 - len(cands5))
    mins = _expand(cands5)
    t0 = idata.open_ts(date)
    return [[t0 + 60 * k, o, h, l, c, 1000.0] for k, (o, h, l, c) in enumerate(mins)]


def _bullish_cands():
    """30-min candle 0 = 100..102; candle 1 wicks to 99.5 and closes at 101 (rejection); then a bearish 5-minute candle (the order block) and a displacement up."""
    c0 = [(101.0, 101.1, 100.9, 101.0)] * 2 + [(101.0, 102.0, 100.0, 101.0)] + [(101.0, 101.1, 100.9, 101.0)] * 3
    c1 = [(101.0, 101.1, 100.9, 101.0)] * 2 + [(101.0, 101.2, 99.5, 100.9)] + [(100.9, 101.1, 100.85, 101.0)] * 3
    return c0 + c1 + [(101.0, 101.05, 100.85, 100.9), (100.9, 102.05, 100.85, 102.0), (102.0, 102.2, 101.9, 102.1)]


def test_three_step_event_found_with_bias_and_not_without():
    date = "2026-10-05"
    bars = _day_from_5m(date, _bullish_cands())
    ev = ss.three_step_events(bars, date, 1)
    assert len(ev) == 1 and ev[0]["candle30"] == 1 and ev[0]["push5"] == 13 and ev[0]["ob5"] == 12, ev
    assert ev[0]["setup_complete_min"] == 60                                          # the setup candle ends 10:15: nothing fires before 09:45
    assert ss.three_step_events(bars, date, 0) == []                                  # no daily bias: no trade
    assert ss.three_step_events(bars, date, -1) == []                                 # a rejection against the bias does not count


def test_three_step_mirror_for_a_bearish_bias():
    date = "2026-10-06"
    mir = [(200 - o, 200 - l, 200 - h, 200 - c) for o, h, l, c in _bullish_cands()]
    ev = ss.three_step_events(_day_from_5m(date, mir), date, -1)
    assert len(ev) == 1 and ev[0]["bias"] == -1 and ev[0]["ob5"] == 12, ev


def test_three_step_needs_a_displacement_and_an_order_block():
    date = "2026-10-07"
    weak = _bullish_cands()
    weak[13] = (100.9, 101.15, 100.85, 101.1)                                         # a small push: no displacement
    assert ss.three_step_events(_day_from_5m(date, weak), date, 1) == []
    no_ob = _bullish_cands()
    for k in range(8, 13):
        no_ob[k] = (101.0, 101.3, 100.9, 101.1)                                       # every candle before the push is green: no order block
    assert ss.three_step_events(_day_from_5m(date, no_ob), date, 1) == []


def test_daily_bias_rules():
    prev2 = (100.0, 102.0, 100.0, 101.0)
    assert ss.daily_bias((100.0, 101.5, 99.0, 100.8), prev2, 2.0) == 1                # pierced the low by 1.0, closed back above it
    assert ss.daily_bias((100.0, 101.5, 99.0, 99.5), prev2, 2.0) == 0                 # closed below: a breakdown
    assert ss.daily_bias((101.0, 103.0, 100.5, 101.5), prev2, 2.0) == -1              # pierced the high, closed back inside
    assert ss.daily_bias((100.0, 101.9, 100.1, 101.0), prev2, 2.0) == 0               # inside day


def test_part_b_counts_and_insufficient_label():
    with tempfile.TemporaryDirectory() as td:
        flat = lambda o, h, l, c: (lambda off: (c, c, c, c))  # noqa: E731
        a = _day_from_5m("2026-10-01", [(101.0, 101.0, 101.0, 101.0)] * 30 + [(101.0, 102.0, 100.0, 101.0)] + [(101.0, 101.0, 101.0, 101.0)] * 44)
        b = _day_from_5m("2026-10-02", [(100.8, 100.8, 100.8, 100.8)] * 30 + [(100.8, 101.0, 99.0, 101.0)] + [(101.0, 101.0, 101.0, 101.0)] * 44)
        c = _day_from_5m("2026-10-05", _bullish_cands())
        for d, bars in (("2026-10-01", a), ("2026-10-02", b), ("2026-10-05", c)):
            _write_day(td, d, "ABC", bars)
        rep = ss.run_part_b(Path(td), 30)
        assert rep["symbol_days_scanned"] == 1 and rep["symbol_days_with_bias"] == 1, rep
        assert rep["events"] == 1 and rep["events_by_side"] == {"long": 1, "short": 0}
        assert rep["verdict"] == "insufficient_sample"                                  # one event is a count, never an edge


def test_no_study_function_reaches_into_live_trading():
    src = (SCRIPTS / "sweep_study.py").read_text()
    for banned in ("requests", "urllib.request", "httpx", "services/", "os.environ", "getenv", "sqlalchemy"):
        assert banned not in src.replace("sweep_study.py", ""), banned               # offline only: no network, no env, no service import


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
