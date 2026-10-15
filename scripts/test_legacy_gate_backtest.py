"""Tests for scripts/legacy_gate_backtest.py (group 380). Plain asserts: python3 scripts/test_legacy_gate_backtest.py  or  pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import legacy_gate_backtest as lg  # noqa: E402

DAY = 86400.0
T0 = 1_700_000_000.0 - (1_700_000_000.0 % DAY)


def _bars(closes, vols=None):
    out = []
    for k, c in enumerate(closes):
        v = 1000.0 if vols is None else vols[k]
        out.append((T0 + k * DAY, c, c * 1.002, c * 0.998, c, v))
    return out


def _rising(n=300, start=50.0, end=100.0):
    return [start + (end - start) * k / (n - 1) for k in range(n)]


def _spike(n, last=5, mult=2.0):
    return [1000.0 * (mult if k >= n - last else 1.0) for k in range(n)]


def test_fresh_confirmed_high_is_exempt_from_both_rules():
    c = _rising()
    f = lg.day_flags(_bars(c, _spike(len(c))), len(c) - 1)
    assert f["r52"] and f["res"] and f["fresh"] and not f["vol"] and not f["down"]


def test_no_volume_confirmation_is_not_fresh():
    c = _rising()
    f = lg.day_flags(_bars(c), len(c) - 1)
    assert f["r52"] and f["res"] and not f["fresh"]


def test_old_high_is_not_fresh_even_with_volume():
    c = _rising(250, 50, 100) + [100.0] * 50              # the high is 50 sessions old, price still within 1% of it
    f = lg.day_flags(_bars(c, _spike(len(c))), len(c) - 1)
    assert f["r52"] and not f["fresh"]


def test_downtrend_flag_and_quiet_52w_rule():
    c = _rising(300, 100.0, 70.0)                          # -30% over the window
    f = lg.day_flags(_bars(c), len(c) - 1)
    assert f["down"] and not f["r52"] and not f["res"]


def test_volume_health_flag():
    c = _rising()
    vols = [1000.0] * 295 + [100.0] * 5                    # the last 5 sessions dry up
    f = lg.day_flags(_bars(c, vols), len(c) - 1)
    assert f["vol"]
    assert not lg.day_flags(_bars(c), len(c) - 1)["vol"]


def test_too_little_history_returns_none():
    assert lg.day_flags(_bars(_rising(200)), 199) is None
    assert lg.rows_for(_bars(_rising(100)), "X", 3, 0.3, 0.1) == []


def test_flags_use_only_past_bars():
    c = _rising()
    a = lg.day_flags(_bars(c), 280)
    c2 = c[:281] + [c[280] * 5.0] * (len(c) - 281)           # rewrite the future
    assert lg.day_flags(_bars(c2), 280) == a


def test_rows_price_next_open_with_slippage_and_cost():
    c = [100.0] * 300
    rows = lg.rows_for(_bars(c), "X", 10, 0.30, 0.10)
    assert rows and all(set(r) >= {"sym", "month", "r1", "r5", "r10", "r20", "down", "r52", "res", "vol", "fresh"} for r in rows)
    # flat price: entry = 100 * 1.001, exit 100 -> -0.0999 - 0.30
    assert abs(rows[0]["r10"] - (-0.0999 - 0.30)) < 0.01


def _synthetic(good_gate_rejects_bad: bool, n_syms=30, n_months=12):
    rows = []
    for s in range(n_syms):
        for m in range(n_months):
            for k in range(10):
                rej = k % 2 == 0
                ret = (-1.0 if rej else 1.0) if good_gate_rejects_bad else (1.0 if rej else -1.0)
                ret += 0.01 * ((s + m + k) % 5)
                rows.append({"sym": f"S{s}", "month": f"2025-{m + 1:02d}", "down": rej, "r52": False, "res": False, "vol": False, "fresh": False,
                             "r1": ret, "r5": ret, "r10": ret, "r20": ret})
    return rows


def test_gate_that_removes_bad_days_helps_and_the_reverse_hurts():
    g = lg.gate_report(_synthetic(True), 10, 50, 200)["downtrend_6m"]
    assert g["verdict"] == "helps" and g["edge_pp"] > 1.9 and g["ci90"][0] > 0
    g = lg.gate_report(_synthetic(False), 10, 50, 200)["downtrend_6m"]
    assert g["verdict"] == "hurts" and g["edge_pp"] < -1.9 and g["ci90"][1] < 0


def test_small_sample_is_insufficient_and_never_a_verdict():
    g = lg.gate_report(_synthetic(True, n_syms=2, n_months=3), 10, 200, 200)["downtrend_6m"]
    assert g["verdict"] == "insufficient_sample"
    e = lg.gate_report([], 10, 10, 50)["downtrend_6m"]
    assert e["verdict"] == "insufficient_sample" and e["edge_pp"] is None and e["population"] == 0


def test_exemption_test_compares_exempted_with_still_rejected_among_legacy_rejects():
    rows = []
    for s in range(30):
        for m in range(12):
            for k in range(6):
                fresh = k < 2
                rows.append({"sym": f"S{s}", "month": f"2025-{m + 1:02d}", "down": False, "r52": True, "res": True, "vol": False, "fresh": fresh,
                             "r1": 0, "r5": 0, "r20": 0, "r10": (2.0 if fresh else 0.0) + 0.01 * ((s + m + k) % 4)})
    g = lg.gate_report(rows, 10, 50, 200)
    assert g["exemption_test"]["population"] == len(rows) and g["exemption_test"]["edge_pp"] > 1.9
    assert g["range_52w_with_exemption"]["rejected"] < g["range_52w_legacy"]["rejected"]


def _write_hist(d: Path, names):
    for name in names:
        p, out, dt = 50.0, [], date(2022, 1, 3)
        for k in range(330):
            p *= 1.001 + (0.012 if k % 9 == 0 else -0.004)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
            out.append({"date": dt.isoformat(), "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000 + (k % 7) * 100})
            dt += timedelta(days=1)
        (d / f"{name}.json").write_text(json.dumps({"candles": out}))


def test_cli_end_to_end_is_seeded_and_skips_the_benchmark():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write_hist(d, ["^NSEI"] + [f"S{k:02d}" for k in range(8)])
        reps = []
        for out in ("o1", "o2"):
            rc = lg.main([td, "--out", str(d / out), "--step", "5", "--min-n", "5", "--boot", "50", "--sample", "4", "--seed", "3"])
            assert rc == 0
            reps.append(json.loads((d / out / "gate_report.json").read_text()))
        assert reps[0] == reps[1]
        assert reps[0]["meta"]["sample"] == {"requested": 4, "pool": 8, "drawn": 4, "seed": 3} and reps[0]["days"] > 0
        assert set(reps[0]["gates"]) == {n for n, _, _ in lg.GATES}
        md = (d / "o1" / "gate_report.md").read_text()
        assert "random seed 3" in md and "exemption_test" in md
        assert lg.main([td, "--out", str(d / "o3"), "--step", "5", "--min-n", "5", "--boot", "50", "--jobs", "2"]) == 0
        assert json.loads((d / "o3" / "gate_report.json").read_text())["meta"]["sample"]["seed"] is None


def test_empty_folder_exits_nonzero():
    with tempfile.TemporaryDirectory() as td:
        assert lg.main([td, "--boot", "50"]) == 1


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"ok {n} tests")
