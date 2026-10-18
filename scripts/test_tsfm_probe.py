"""Tests for scripts/tsfm_probe.py (group 434; plain asserts, also collected by pytest). Synthetic data only: a latent volatility that the baselines
see through noisy trailing windows and a fake 'model' that knows it, so Gate A can be shown to pass when there is information and to NOT pass when there is none."""
import json
import math
import os
import random
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tsfm_probe as p  # noqa: E402


def _synth(n_sym=10, n_bars=760, seed=1):
    """{sym: bars}, latent {(sym, date): (sigma, volume_level)}"""
    rng = random.Random(seed)
    data, latent = {}, {}
    for s in range(n_sym):
        sym = f"S{s:02d}"
        d, bars, ls, lv, c = date(2021, 1, 4), [], 0.0, 0.0, 100.0
        while len(bars) < n_bars:
            if d.weekday() < 5:
                ls = 0.97 * ls + 0.25 * rng.gauss(0, 1)
                lv = 0.9 * lv + 0.2 * rng.gauss(0, 1)
                sig = 0.01 * math.exp(ls)
                z = rng.gauss(0, 1)
                o = c
                c = o * math.exp(sig * z)
                hi = max(o, c) * (1 + sig * abs(rng.gauss(0, 1)) * 0.5)
                lo = min(o, c) * (1 - sig * abs(rng.gauss(0, 1)) * 0.5)
                vol = 1e6 * math.exp(lv) * math.exp(0.15 * rng.gauss(0, 1))
                bars.append({"date": d.isoformat(), "open": o, "high": hi, "low": lo, "close": c, "volume": vol})
                latent[(sym, d.isoformat())] = (sig, 1e6 * math.exp(lv))
            d += timedelta(days=1)
        data[sym] = bars
    return data, latent


DATA, LATENT = _synth()


def _informed(sym, bars):
    sig, vol = LATENT[(sym, bars[-1]["date"])]
    return sig * 100.0 * 2.5, vol                           # knows today's latent state (no future information)


def _noise(sym, bars):
    r = random.Random(f"{sym}{bars[-1]['date']}")
    return r.uniform(0.5, 6.0), r.uniform(5e5, 3e6)


def _run(models, **kw):
    dec = p.build_decisions(DATA, 5, 60, 7, 300, models)
    return p.evaluate(dec, 5, list(models), 300, 7, kw.get("holdout_months", 0.0), kw.get("holdout_only", False), 150, min_n=100), dec


def test_pinball_and_quantile_and_ols():
    assert p.pinball(10, 8, 0.9) == 1.8 and abs(p.pinball(6, 8, 0.9) - 0.2) < 1e-12 and p.pinball(8, 8, 0.5) == 0
    assert p._quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0 and p._quantile([1.0, 2.0], 0.5) == 1.5
    rng = random.Random(3)
    xs = [(1.0, rng.random(), rng.random(), rng.random()) for _ in range(300)]
    ys = [2.0 + 3.0 * x[1] - 1.0 * x[2] + 0.5 * x[3] for x in xs]
    b = p._ols(xs, ys)
    assert all(abs(u - v) < 1e-6 for u, v in zip(b, (2.0, 3.0, -1.0, 0.5)))


def test_features_at_t_do_not_depend_on_later_bars():
    bars = DATA["S00"]
    t = 450
    a = p.SymbolSeries(bars[:t + 1 + 5], 5)
    b = p.SymbolSeries([dict(x) for x in bars[:t + 1]] + [dict(x, high=x["high"] * 3, close=x["close"] * 2, volume=1e12) for x in bars[t + 1:]], 5)
    for name in ("atr", "ewma", "ewmav", "vol20", "w", "m"):
        assert getattr(a, name)[t] == getattr(b, name)[t], name
    assert a.har_point(t) == b.har_point(t)                  # the HAR fit only uses targets already known at t


def test_baselines_alone_give_a_report_and_no_gate_a():
    rep, dec = _run({})
    assert rep["gate_a"]["status"] == "NO_INPUT" and rep["targets"]["range"]["best_baseline"] in p.RANGE_BASELINES
    assert rep["targets"]["range"]["baseline_vs_atr"]["har"]["n"] > 100
    for nm, s in rep["targets"]["range"]["baselines"].items():
        assert 0.55 < s["coverage_80"] < 0.95, (nm, s)         # calibrated by construction, roughly


def test_an_informed_model_passes_gate_a_and_noise_does_not():
    rep, _ = _run({"informed": _informed, "noise": _noise})
    c = rep["targets"]["range"]["comparisons"]
    assert c["informed"]["verdict"] == p.PASS and c["informed"]["improvement_pct"] > 0 and c["informed"]["ci90"][0] > 0
    assert c["informed"]["halves"]["first"] > 0 and c["informed"]["halves"]["second"] > 0
    assert c["noise"]["verdict"] in (p.HURTS, p.NO_EDGE) and c["noise"]["improvement_pct"] < 0
    assert rep["targets"]["volume"]["comparisons"]["informed"]["verdict"] == p.PASS
    assert rep["gate_a"]["range_verdict"]["informed"] == "pass"


def test_too_few_decisions_is_insufficient_not_pass():
    dec = p.build_decisions({k: DATA[k] for k in list(DATA)[:1]}, 5, 20, 7, 300, {"informed": _informed})
    rep = p.evaluate(dec, 5, ["informed"], 100, 7, 0.0, False, 5, min_n=200)
    assert rep["targets"]["range"]["comparisons"]["informed"]["verdict"] == p.INSUFFICIENT


def test_holdout_is_not_scored_unless_asked_and_calibration_never_uses_the_future():
    dec = p.build_decisions(DATA, 5, 60, 7, 300, {"informed": _informed})
    rep = p.evaluate(dec, 5, ["informed"], 100, 7, 6.0, False, 150, min_n=100)
    cut = date.fromisoformat(rep["meta"]["holdout_cut"]).toordinal()
    assert rep["meta"]["n_scored_pool"] < len(dec)
    pool = [d for d in dec if d["ord"] <= cut]
    rows = p.score_predictor(pool, "atr", "range", 10, 150)
    assert rows and max(r["ord"] for r in rows) <= cut
    ho = p.evaluate(dec, 5, ["informed"], 100, 7, 6.0, True, 150, min_n=50)
    assert ho["meta"]["holdout_only"] is True and ho["targets"]["range"]["common_n"] > 0


def test_embargo_keeps_unknown_outcomes_out_of_calibration():
    rows = [{"id": str(i), "sym": "A", "date": "x", "ord": 1000 + i, "m_atr": 1.0, "y_range": 1.0 if i < 3 else 100.0} for i in range(12)]
    out = p.score_predictor(rows, "atr", "range", embargo_days=5, min_cal=3)
    assert out[0]["ord"] == 1000 + 2 + 5                       # the earliest row with 3 outcomes that were known 5 days before it
    # that row (y = 100) was scored with ratios from the first three rows only (all 1.0), not with the later 100.0 outcomes
    assert abs(out[0]["loss"] - sum(p.pinball(100.0, 1.0, t) for t in p.TAUS) / 3) < 1e-9


def test_combine_needs_two_reports_and_every_one_must_pass():
    def rep(v):
        return {"targets": {"range": {"comparisons": {"m": {"verdict": v, "improvement_pct": 3.0, "ci90": [0.1, 0.5], "halves": {"first": 1, "second": 1}}}},
                            "volume": {"comparisons": {"m": {"verdict": p.PASS, "improvement_pct": 2.0}}}}}
    assert p.combine([rep(p.PASS)])["status"] == "NOT_ENOUGH_DATA"
    assert p.combine([rep(p.PASS), rep(p.PASS)])["status"] == "PASS"
    assert p.combine([rep(p.PASS), rep(p.NO_EDGE)])["status"] == "FAIL"
    assert p.combine([rep(p.PASS), rep(p.INSUFFICIENT)])["status"] == "NOT_ENOUGH_DATA"
    assert p.combine([])["status"] == "NO_INPUT" and p.combine([{"targets": {}}])["status"] == "NO_INPUT"
    assert p.combine([rep(p.PASS), rep(p.PASS)])["models"]["m"]["volume_target_pass"] is True


def test_cli_no_model_writes_reports(tmp_path):
    h = tmp_path / "hist"
    h.mkdir()
    for sym in list(DATA)[:6]:
        (h / f"{sym}.json").write_text(json.dumps({"candles": DATA[sym]}))
    (h / "NIFTY.json").write_text(json.dumps({"candles": DATA["S00"]}))         # the benchmark file is skipped
    rc = p.main([str(h), "--out", str(tmp_path / "o"), "--no-model", "--per-symbol", "50", "--min-cal", "100", "--boot", "100", "--holdout-months", "0"])
    assert rc == 0 and (tmp_path / "o" / "probe_report.json").exists() and "Gate A" in (tmp_path / "o" / "probe_report.md").read_text()
    assert "NIFTY" not in p.load_dir(h)
    assert p.main([str(tmp_path / "empty")]) == 2


def test_http_model_sends_only_bars_up_to_the_decision_day_and_caches(tmp_path):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            out = json.dumps({"fan_range_pct": 4.2, "volume_forecast": 123456.0}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cf = tmp_path / "c.jsonl"
        cache = {}
        fn = p.http_model(f"http://127.0.0.1:{srv.server_port}", 5, 10, cache, cf)
        bars = DATA["S00"][:400]
        assert fn("S00", bars) == (4.2, 123456.0) and fn("S00", bars) == (4.2, 123456.0)
        assert len(seen) == 1 and seen[0]["candles"][-1]["date"] == bars[-1]["date"] and seen[0]["horizon"] == 5
        assert p.load_cache(cf) == {f"S00|{bars[-1]['date']}|5": {"r": 4.2, "v": 123456.0}}
    finally:
        srv.shutdown()


def test_a_failing_model_call_is_skipped_not_fatal():
    def boom(sym, bars):
        raise RuntimeError("down")
    dec = p.build_decisions({"S00": DATA["S00"]}, 5, 10, 7, 300, {"m": boom})
    assert dec and all(d["m_m"] is None for d in dec)


def test_max_forecasts_limits_model_calls():
    n = []

    def fn(sym, bars):
        n.append(1)
        return 1.0, 1.0
    p.build_decisions(DATA, 5, 20, 7, 300, {"m": fn}, max_forecasts=7)
    assert len(n) == 7
