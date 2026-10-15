"""Tests for scripts/ms_ab_report.py and scripts/fetch_history_for_backtest.py (group 375).

Run: python3 scripts/test_ms_ab_report.py   (plain asserts, no pytest needed)  or  python3 -m pytest scripts/test_ms_ab_report.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import fetch_history_for_backtest as fh  # noqa: E402
import ms_ab_report as ab  # noqa: E402

DAY = 86400.0
T0 = 1_700_000_000.0 - (1_700_000_000.0 % DAY)  # a 00:00 UTC stamp


def _bars(n, o=100.0, c=100.0, h=101.0, l=99.0):
    return [(T0 + k * DAY, o, h, l, c, 1000.0) for k in range(n)]


def _fake_replay(summary, quality="ok", at=(130,)):
    def rp(bars, step=1, **kw):
        return [{"i": i, "ts": bars[i][0], "price": bars[i][4], "quality": quality, "summary": dict(summary)} for i in at]
    return rp


GOOD = {"ms_stretch_atr": 1.0, "ms_stop": 95.0, "ms_target": 120.0, "ms_chase_atr": 0.5, "ms_room_atr": 2.0, "ms_rr": 2.0,
        "ms_rr_flags": [], "ms_trend": "bullish"}


def test_entry_is_next_open_plus_slippage_and_cost_is_subtracted():
    bars = _bars(200)
    bars[131] = (bars[131][0], 100.0, 101.0, 99.0, 100.0, 1000.0)  # entry session: open 100
    orig = ab.replay
    ab.replay = _fake_replay(GOOD)
    try:
        rows = ab._rows(bars, "X", 1, {}, 0.30, 0.10, None)
    finally:
        ab.replay = orig
    assert len(rows) == 1
    r = rows[0]
    # entry = 100 * 1.001 = 100.1 ; T+1 close = bars[131] close = 100 -> (100/100.1-1)*100 = -0.0999 ; minus 0.30 cost
    assert abs(r["r1"] - (-0.0999 - 0.30)) < 0.01, r["r1"]
    assert r["rej"] == []


def test_baseline_filter_and_incomplete_outcomes_are_dropped():
    orig = ab.replay
    try:
        ab.replay = _fake_replay(dict(GOOD, ms_stretch_atr=3.4))  # extended: not in the baseline
        assert ab._rows(_bars(200), "X", 1, {}, 0.3, 0.1, None) == []
        ab.replay = _fake_replay(dict(GOOD, ms_stretch_atr=-0.2))  # below EMA20
        assert ab._rows(_bars(200), "X", 1, {}, 0.3, 0.1, None) == []
        ab.replay = _fake_replay(GOOD, at=(185,))  # fewer than 20 sessions after entry
        assert ab._rows(_bars(200), "X", 1, {}, 0.3, 0.1, None) == []
        ab.replay = _fake_replay(GOOD, quality="insufficient")
        assert ab._rows(_bars(200), "X", 1, {}, 0.3, 0.1, None) == []
        ab.replay = _fake_replay(GOOD, at=(199,))  # no next session at all
        assert ab._rows(_bars(200), "X", 1, {}, 0.3, 0.1, None) == []
    finally:
        ab.replay = orig


def test_rule_flags_and_same_bar_stop_first():
    bars = _bars(200, h=125.0, l=94.0)  # every bar touches both the stop (95) and the target (120)
    orig = ab.replay
    ab.replay = _fake_replay(dict(GOOD, ms_rr=0.8, ms_chase_atr=2.5))
    try:
        r = ab._rows(bars, "X", 1, {}, 0.3, 0.1, None)[0]
    finally:
        ab.replay = orig
    assert sorted(r["rej"]) == ["chase", "rr"]
    assert r["stop_first"] is True and r["target_first"] is False


def test_nifty_relative_and_regime_tags():
    bars = _bars(300)
    bench = ab.bench_info(_bars(300, o=200.0, c=220.0, h=225.0, l=195.0))
    assert bench["bull"][-1] is not None  # 200-day SMA exists by bar 300
    orig = ab.replay
    ab.replay = _fake_replay(GOOD, at=(230,))
    try:
        r = ab._rows(bars, "X", 1, {}, 0.3, 0.1, bench)[0]
    finally:
        ab.replay = orig
    # bench open 200 -> close 220 = +10% ; stock ~ -0.4% net => relative about -10.4
    assert r["x1"] is not None and -12 < r["x1"] < -8, r["x1"]
    assert r["bull"] in (True, False)


def _row(sym, month, ts, r, rej):
    return {"sym": sym, "month": month, "ts": ts, "rej": rej, "stop_first": False, "target_first": False, "mae": -1.0,
            "bull": True, "hv": False, **{f"r{h}": r for h in ab.HORIZONS}, **{f"x{h}": None for h in ab.HORIZONS}}


def _synthetic(edge_good: bool, n_clusters=60):
    rows = []
    for c in range(n_clusters):
        for k in range(6):
            ts = 1_600_000_000.0 + c * 40 * DAY + k * DAY
            kept = (k % 2 == 0)
            r = (2.0 if kept else -1.0) if edge_good else (-1.0 if kept else 2.0)
            rows.append(_row(f"S{c % 15}", f"m{c}", ts, r, [] if kept else ["room"]))
    return rows


def test_verdict_pass_hurts_and_insufficient():
    meta = {"symbols": 1, "skipped": 0, "benchmark": "x", "step": 1, "cost_pct": 0.3, "slip_pct": 0.1}
    good = ab.build_report(_synthetic(True), 10, 50, 200, meta)
    assert good["verdict"] == "pass" and good["edge_pp"] > 0 and good["halves"]["first"] > 0 and good["halves"]["second"] > 0, good
    bad = ab.build_report(_synthetic(False), 10, 50, 200, meta)
    assert bad["verdict"] == "hurts" and bad["edge_pp"] < 0, bad
    thin = ab.build_report(_synthetic(True, n_clusters=10), 10, 200, 200, meta)
    assert thin["verdict"] == "insufficient_sample"
    assert ab.build_report([], 10, 50, 100, meta)["verdict"] == "insufficient_sample"


def test_pass_requires_both_halves_positive():
    meta = {"symbols": 1, "skipped": 0, "benchmark": "x", "step": 1, "cost_pct": 0.3, "slip_pct": 0.1}
    rows = _synthetic(True)
    cut = sorted(r["ts"] for r in rows)[len(rows) // 2]
    for r in rows:  # make the second half reward the rejected days instead
        if r["ts"] >= cut:
            for h in ab.HORIZONS:
                r[f"r{h}"] = -r[f"r{h}"]
    rep = ab.build_report(rows, 10, 50, 200, meta)
    assert rep["verdict"] != "pass", rep["verdict"]


def test_bootstrap_is_deterministic():
    rows = _synthetic(True)
    assert ab.bootstrap_edge(rows, 10, 100) == ab.bootstrap_edge(rows, 10, 100)


def test_markdown_renders_all_sections():
    meta = {"symbols": 3, "skipped": 0, "benchmark": "^NSEI", "step": 3, "cost_pct": 0.3, "slip_pct": 0.1}
    md = ab.to_markdown(ab.build_report(_synthetic(True), 10, 50, 100, meta))
    for s in ("Verdict", "By horizon", "Rejected by each rule", "By market regime", "profit factor"):
        assert s in md, s


def _candles(n, p0=100.0):
    out, p = [], p0
    for k in range(n):
        p *= 1.001 + (0.01 if k % 7 == 0 else -0.004)
        d = f"{2021 + k // 300}-{1 + (k % 300) // 25:02d}-{1 + k % 25:02d}"
        out.append({"date": d, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000})
    return out


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/universe":
            body, code = json.dumps(["AAA", "BBB.NS", "../EVIL", "GONE", "AAA"]).encode(), 200
        elif path.startswith("/history/"):
            sym = path.split("/")[-1]
            if sym == "GONE":
                body, code = b"{}", 404
            else:
                body, code = json.dumps({"candles": _candles(300)}).encode(), 200
        else:
            body, code = b"{}", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def test_fetcher_saves_benchmark_symbols_and_skips_dead_and_unsafe_names():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with tempfile.TemporaryDirectory() as td:
            code = fh.main([td, "--base", base, "--universe-url", base + "/universe", "--sleep", "0", "--retries", "1",
                            "--min-bars", "250", "--max-failures-pct", "50"])
            files = sorted(p.name for p in Path(td).iterdir())
            assert files == ["AAA.json", "BBB.json", "^NSEI.json"], files  # dup / .NS suffix / path traversal / 404 handled
            assert code == 0
            mt = (Path(td) / "AAA.json").stat().st_mtime_ns
            fh.main([td, "--base", base, "--symbols", "AAA", "--sleep", "0", "--retries", "1"])
            assert (Path(td) / "AAA.json").stat().st_mtime_ns == mt  # resume keeps good files
            # too many failures -> non-zero so a workflow does not report on a hollow dataset
            assert fh.main([td, "--base", base, "--symbols", "GONE", "--sleep", "0", "--retries", "1"]) == 1
    finally:
        srv.shutdown()


def test_fetcher_needs_a_base_url():
    assert fh.main(["/tmp/_x_hist", "--base", "", "--symbols", "A"]) == 2


def test_cli_end_to_end_with_process_pool():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name in ("^NSEI", "AAA", "BBB"):
            (d / f"{name}.json").write_text(json.dumps({"candles": _candles(300)}))
        rc = ab.main([td, "--min-n", "5", "--boot", "50", "--step", "10", "--jobs", "2"])
        assert rc in (0, 1)
        rep = json.loads((d / "ab_out" / "ab_report.json").read_text())
        assert rep["verdict"] in ("insufficient_sample", "pass", "hurts", "no_edge")
        assert (d / "ab_out" / "ab_report.md").exists()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"ok {n} tests")
