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
import structure_backtest as sb  # noqa: E402

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


def test_structure_backtest_net_samples_match_ab_pricing():
    bars = _bars(200)
    bars[131] = (bars[131][0], 100.0, 101.0, 99.0, 100.0, 1000.0)
    orig = sb.replay
    sb.replay = _fake_replay(GOOD)
    try:
        s = sb.net_samples(bars, 1, 0.30, 0.10)
    finally:
        sb.replay = orig
    assert len(s) == 1
    assert abs(s[0]["outcome"]["ret_1d"] - (-0.0999 - 0.30)) < 0.01  # same number as ms_ab_report's r1
    assert abs(s[0]["price"] - 100.1) < 1e-9


def test_structure_backtest_load_skips_the_benchmark():
    with tempfile.TemporaryDirectory() as td:
        for name in ("^NSEI", "AAA"):
            (Path(td) / f"{name}.json").write_text(json.dumps({"candles": _candles(300)}))
        assert [k for k, _ in sb.load(Path(td))] == ["AAA"]


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
    assert ab.build_report([], 10, 50, 100, meta)["verdict"] == "no_data"


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


def test_by_rule_solo_and_drop_one_edges():
    meta = {"symbols": 1, "skipped": 0, "benchmark": "x", "step": 1, "cost_pct": 0.3, "slip_pct": 0.1}
    rows = []
    for k in range(40):  # "room" rejects the bad days; "rr" rejects random good ones, so only room earns its place
        rows.append(_row("S", f"m{k}", 1e9 + k, 2.0, []))
        rows.append(_row("S", f"m{k}", 1e9 + k, -2.0, ["room"]))
        rows.append(_row("S", f"m{k}", 1e9 + k, 2.0, ["rr"]))
    rep = ab.build_report(rows, 10, 10, 50, meta)
    room, rr = rep["by_rule"]["room"], rep["by_rule"]["rr"]
    assert room["solo_edge_pp"] > 0 and room["drop_one_edge_pp"] > 0, room
    assert rr["drop_one_edge_pp"] is not None and rr["drop_one_edge_pp"] <= 0.0001, rr  # dropping rr adds the good days back
    assert "drop-one edge" in ab.to_markdown(rep)


def test_fetcher_sample_is_random_seeded_and_not_the_alphabetical_head():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    names = [f"S{k:03d}" for k in range(60)]
    try:
        picked = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                fh.main([td, "--base", base, "--symbols", ",".join(names), "--sample", "10", "--seed", "3", "--sleep", "0", "--retries", "1"])
                picked.append(sorted(p.stem for p in Path(td).iterdir() if p.stem != "^NSEI"))
        assert picked[0] == picked[1] and len(picked[0]) == 10  # deterministic
        assert picked[0] != names[:10]  # not the head of the list
    finally:
        srv.shutdown()


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


def test_draw_sample_is_independent_of_input_order_and_reproducible():
    names = [f"S{k:03d}" for k in range(60)]
    a = fh.draw_sample(names, 10, 3)
    b = fh.draw_sample(list(reversed(names)), 10, 3)
    c = fh.draw_sample(names + names[:5], 10, 3)  # duplicates in the universe do not change the draw
    assert a == b == c and len(a) == 10 and a == sorted(a)
    assert fh.draw_sample(names, 10, 4) != a              # another seed, another sample
    assert fh.draw_sample(names, 0, 3) == sorted(names)   # no sample = the whole sorted list
    assert fh.draw_sample(names, 60, 3) == sorted(names)  # asking for more than exists = everything
    big = [f"T{k:04d}" for k in range(1500)]
    s400 = fh.draw_sample(big, 400, 7)
    assert len(s400) == 400 and len(set(s400)) == 400 and s400 != sorted(big)[:400]


def test_fetcher_writes_a_replayable_manifest_outside_the_history_folder():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    names = [f"S{k:03d}" for k in range(40)]
    try:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as md:
            man = Path(md) / "sample.json"
            fh.main([td, "--base", base, "--symbols", ",".join(names), "--sample", "8", "--seed", "5", "--manifest", str(man),
                     "--sleep", "0", "--retries", "1"])
            doc = json.loads(man.read_text())
            assert doc["seed"] == 5 and doc["requested_sample"] == 8 and doc["universe_size"] == 40 and doc["drawn"] == 8
            assert doc["symbols"] == fh.draw_sample(names, 8, 5)
            saved = sorted(p.stem for p in Path(td).iterdir() if p.stem != "^NSEI")
            assert saved == doc["symbols"]                      # exactly the manifest was downloaded
            assert not any(p.name == "sample.json" for p in Path(td).iterdir())
        # an unwritable manifest path stops the run before any download
        with tempfile.TemporaryDirectory() as td:
            assert fh.main([td, "--base", base, "--symbols", "AAA", "--manifest", "/nonexistent_dir_g379/m.json",
                            "--sleep", "0", "--retries", "1"]) == 2
            assert list(Path(td).iterdir()) == []
    finally:
        srv.shutdown()


def test_report_sample_is_seeded_recorded_and_skips_the_benchmark():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name in ["^NSEI"] + [f"S{k:02d}" for k in range(12)]:
            (d / f"{name}.json").write_text(json.dumps({"candles": _candles(300)}))
        outs = []
        for out in ("o1", "o2"):
            ab.main([td, "--out", str(d / out), "--min-n", "5", "--boot", "20", "--step", "30", "--sample", "5", "--seed", "11"])
            outs.append(json.loads((d / out / "ab_report.json").read_text()))
        for rep in outs:
            assert rep["meta"]["sample"] == {"requested": 5, "pool": 12, "drawn": 5, "seed": 11}
            assert rep["meta"]["symbols"] + rep["meta"]["skipped"] == 5
        assert outs[0]["meta"] == outs[1]["meta"] and outs[0]["signals"] == outs[1]["signals"]   # same seed, same sample, same numbers
        assert "random seed 11" in (d / "o1" / "ab_report.md").read_text()
        ab.main([td, "--out", str(d / "o3"), "--min-n", "5", "--boot", "20", "--step", "30"])      # no --sample: every file, no sample line
        rep = json.loads((d / "o3" / "ab_report.json").read_text())
        assert rep["meta"]["sample"] == {"requested": 0, "pool": 12, "drawn": 12, "seed": None}
        assert "random seed" not in (d / "o3" / "ab_report.md").read_text()


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
        assert rep["verdict"] in ("no_data", "insufficient_sample", "pass", "hurts", "no_edge")
        assert (d / "ab_out" / "ab_report.md").exists()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"ok {n} tests")


def test_group433_universe_read_is_retried(monkeypatch):
    import argparse
    calls = []

    def flaky(url, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionResetError("Connection reset by peer")
        return ["tcs", "INFY.NS"]

    monkeypatch.setattr(fh, "_get_json", flaky)
    monkeypatch.setattr(fh.time, "sleep", lambda s: None)
    a = argparse.Namespace(symbols="", symbols_file="", universe_url="http://gw/api/universe", universe_retries=5, universe_wait=0.0)
    assert fh.read_symbols(a) == ["TCS", "INFY"] and len(calls) == 3
    calls.clear()
    monkeypatch.setattr(fh, "_get_json", lambda url, timeout: (_ for _ in ()).throw(ConnectionRefusedError("down")))
    a.universe_retries = 2
    import pytest
    with pytest.raises(ConnectionRefusedError):
        fh.read_symbols(a)
