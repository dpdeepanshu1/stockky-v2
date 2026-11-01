"""Tests for scripts/setup_backtest.py on synthetic bars. Plain runner: python3 scripts/test_setup_engine.py"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import setup_backtest as sb  # noqa: E402
import _intraday_data as idata  # noqa: E402

def test_bootstrap_and_summarize():
    pairs = [(f"d{i}", 0.5 + 0.01 * i) for i in range(10) for _ in range(5)]
    ci = sb.bootstrap(pairs, 500); assert ci["lo"] > 0
    assert sb.bootstrap([("d1", 1.0)], 100) == {"lo": None, "hi": None}
    rows = [{"date": f"d{i % 8}", "setup": sb.se.S2, "side": "LONG", "slot": "0930", "net": 0.4} for i in range(40)]
    r = sb.summarize(rows, 30, 5, 300)[f"{sb.se.S2}_LONG"]; assert r["n"] == 40 and r["verdict"] in ("edge", "unclear", "insufficient_sample")
    neg = [dict(x, net=-0.4) for x in rows]; neg[0]["net"] = -0.5
    assert sb.summarize(neg, 30, 5, 300)[f"{sb.se.S2}_LONG"]["verdict"] == "no_edge"
    assert sb.summarize([], 30, 5)[f"{sb.se.S1}_LONG"]["n"] == 0

def test_candle_and_clock():
    assert sb._candle((0, 100, 100.1, 98, 100.05, 1)) == 1 and sb._candle((0, 100, 102, 99.95, 100.05, 1)) == -1 and sb._candle((0, 100, 100, 100, 100, 1)) == 0
    c = sb._clock("2026-10-30", 6); assert (c.hour, c.minute) == (9, 21)

def test_replay_no_events_means_no_s1_s4():
    with tempfile.TemporaryDirectory() as td:
        for d in ("2026-10-28", "2026-10-29", "2026-10-30"):
            t0 = idata.open_ts(d)
            for sym, slope in (("AAA", 0.02), ("BBB", -0.02), ("CCC", 0.0)):
                bars, px = [], 100.0
                for m in range(375):
                    o = px; px = px * (1 + slope / 100.0); bars.append([t0 + 60 * m, o, max(o, px) * 1.0005, min(o, px) * 0.9995, px, 1000 + m])
                p = Path(td) / d; p.mkdir(exist_ok=True); (p / f"{sym}.json").write_text(json.dumps({"bars": bars}))
        data = idata.load_dir(td)
        trades = sb.replay(data, since=None, cost_pct=0.15, hold=30)
        assert all(t["setup"] not in (sb.se.S1, sb.se.S4) for t in trades)
        ev = {"2026-10-30": {"AAA": {"polarity": 1, "strength": 0.8, "priced_in": 0.3}}}
        t2 = sb.replay(data, since=None, cost_pct=0.15, hold=30, events=ev)
        assert all(t["setup"] != sb.se.S4 for t in t2)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("ok", n)
