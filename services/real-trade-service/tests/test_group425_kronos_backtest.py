"""group 425: scripts/kronos_backtest.py - ATR stop/target, realized outcome, no-look-ahead day draw, summary verdicts, resumable end-to-end run
with an injected fake worker. No pytest fixtures (tmp dirs are made by hand), so it runs under pytest and a plain loop."""
import importlib.util
import json
import os
import random
import tempfile
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve()
_spec = importlib.util.spec_from_file_location("kronos_backtest", _HERE.parents[3] / "scripts" / "kronos_backtest.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)


def _bars(n, px=100.0, drift=0.0005, start=date(2024, 1, 1)):
    out, d, p = [], start, px
    while len(out) < n:
        if d.weekday() < 5:
            o = p
            p *= 1 + drift
            out.append({"date": d.isoformat(), "open": o, "high": max(o, p) * 1.01, "low": min(o, p) * 0.99, "close": p, "volume": 1000.0})
        d += timedelta(days=1)
    return out


def test_atr_and_stop_target_follow_the_entry_rules():
    b = _bars(40)
    a = kb.atr_pct(b)
    assert a is not None and 1.5 < a < 3.0
    assert kb.atr_pct(_bars(10)) is None
    assert kb.stop_target_pct(1.0) == (2.0, 4.0)          # floor 2 %, target = 2 x stop
    assert kb.stop_target_pct(2.0) == (3.0, 6.0)
    assert kb.stop_target_pct(9.0) == (6.0, 12.0)         # cap 6 %
    assert kb.stop_target_pct(None) == (3.0, 6.0)


def _fut(*rows):
    return [{"open": o, "high": h, "low": l, "close": c, "date": "x", "volume": 0} for o, h, l, c in rows]


def test_realized_outcome_cases():
    assert kb.realized_outcome(_fut((100, 101, 99, 100), (100, 107, 99, 106)), 100.0, 3.0, 6.0) == (6.0, "target")
    assert kb.realized_outcome(_fut((100, 101, 96, 98)), 100.0, 3.0, 6.0) == (-3.0, "stop")
    assert kb.realized_outcome(_fut((100, 107, 96, 100)), 100.0, 3.0, 6.0) == (-3.0, "stop")      # both inside one bar: stop first
    r, how = kb.realized_outcome(_fut((100, 101, 99, 100), (94, 95, 93, 94)), 100.0, 3.0, 6.0)     # gap through the stop exits at the open
    assert how == "stop" and abs(r + 6.0) < 1e-9
    r, how = kb.realized_outcome(_fut((100, 101, 99, 100), (100, 102, 99, 101)), 100.0, 3.0, 6.0)
    assert how == "time" and abs(r - 1.0) < 1e-9


def test_draw_days_leave_history_before_and_outcome_after():
    rng = random.Random(1)
    days = kb.draw_days(400, 20, 5, 250, rng)
    assert len(days) == 20 and days == sorted(days) and min(days) >= 250 and max(days) + 5 <= 400
    assert kb.draw_days(254, 5, 5, 250, random.Random(1)) == []
    assert kb.draw_days(255, 5, 5, 250, random.Random(1)) == [250]
    assert len(kb.draw_days(300, 99, 5, 250, random.Random(1))) == 46


def test_bootstrap_and_summary_verdicts():
    assert kb.bootstrap_diff([1.0] * 3, [1.0] * 10) is None
    lo, hi = kb.bootstrap_diff([-3.0, -2.5, -3.5, -2.0, -3.0, -2.8, -3.2], [1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.3, 0.7])
    assert hi < 0
    rows = ([{"opinion": "ok", "would_reject": True, "net_pct": x} for x in (-3.0, -2.5, -3.5, -2.0, -3.0, -2.8)]
            + [{"opinion": "ok", "would_reject": False, "net_pct": x} for x in (1.0, 1.2, 0.8, 1.1, 0.9, 1.0)])
    s = kb.summarize(rows)
    assert s["n_vetoed"] == 6 and s["n_passed"] == 6 and s["verdict"].startswith("VETO HELPS")
    rows2 = ([{"opinion": "ok", "would_reject": True, "net_pct": x} for x in (-3.0, -2.5, -3.5, -2.0, -3.0, -2.8)]
             + [{"opinion": "ok", "would_reject": False, "net_pct": x} for x in (-0.5, -0.4, -0.6, -0.5, -0.45, -0.55)])
    assert kb.summarize(rows2)["verdict"].startswith("VETO SEPARATES")
    same = [{"opinion": "ok", "would_reject": v, "net_pct": x} for v in (True, False) for x in (1.0, -1.0, 0.5, -0.5, 0.2, -0.2)]
    assert kb.summarize(same)["verdict"].startswith("NOT PROVEN")
    assert kb.summarize([])["verdict"] == "NO DATA"
    assert kb.summarize([{"opinion": "no_opinion", "would_reject": False, "net_pct": 1.0}])["verdict"] == "NO DATA"


def test_verdict_for_uses_the_live_gate_with_cli_thresholds():
    fc = {"n_paths": 10, "ev_pct": 0.2, "p_target_first": 0.3}
    assert kb.verdict_for(fc, 0.35, 1.0, 0.10)["would_reject"] is True
    assert kb.verdict_for(fc, 0.35, 0.0, 0.0)["would_reject"] is False
    assert kb.verdict_for(None, 0.35, 1.0, 0.10)["opinion"] == "no_opinion"
    import config
    assert config.KRONOS_MIN_EDGE_X_COST == 1.0 and config.KRONOS_MIN_P_TARGET == 0.10     # defaults are restored after the call


def test_end_to_end_run_no_lookahead_and_resume():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    for sym in ("AAA", "BBB"):
        (tmp / "data" / f"{sym}.json").write_text(json.dumps({"candles": _bars(320)}))
    (tmp / "data" / "^NSEI.json").write_text(json.dumps({"candles": _bars(320)}))          # the benchmark file is skipped
    seen = []

    def fake(worker, sym, history, ref, sp, tp, horizon, paths, timeout):
        seen.append((sym, history[-1]["date"], ref, len(history)))
        return {"n_paths": paths, "ev_pct": 1.0 if sym == "AAA" else -1.0, "p_target_first": 0.3}

    args = Namespace(dir=str(tmp / "data"), worker="http://none", per_symbol=3, seed=7, horizon=5, paths=10, cost_pct=0.35,
                     min_edge_x_cost=1.0, min_p_target=0.10, min_bars=250, max_forecasts=0, timeout=1.0, out=str(tmp / "out.jsonl"))
    assert kb.run(args, forecast_fn=fake) == 0
    rows = [json.loads(x) for x in (tmp / "out.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and {r["symbol"] for r in rows} == {"AAA", "BBB"}
    for sym, last_hist_date, ref, n_hist in seen:
        bars = kb.load_symbol(tmp / "data" / f"{sym}.json")
        t = next(i for i, b in enumerate(bars) if b["date"] > last_hist_date)
        assert bars[t - 1]["date"] == last_hist_date and ref == bars[t]["open"] and n_hist == t      # only bars BEFORE the decision day
    assert all(r["would_reject"] == (r["symbol"] == "BBB") for r in rows)
    assert all(abs(r["net_pct"] - (r["gross_pct"] - 0.35)) < 1e-3 for r in rows)
    n_calls = len(seen)
    assert kb.run(args, forecast_fn=fake) == 0                       # second run: everything is already in the file
    assert len(seen) == n_calls and len((tmp / "out.jsonl").read_text().splitlines()) == 6
    args.per_symbol, args.max_forecasts = 5, 2                       # more days wanted: only the new ones are forecast, capped
    kb.run(args, forecast_fn=fake)
    assert len(seen) == n_calls + 2
