"""Tests for scripts/entry_rules_backtest.py. Plain asserts: python3 scripts/test_entry_rules_backtest.py  or  pytest."""
from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entry_rules_backtest as er  # noqa: E402


def _bar(d, o, h, l, c, v=1_000_000.0):
    ts = datetime(d.year, d.month, d.day, 4, 0, tzinfo=timezone.utc).timestamp()
    return (ts, o, h, l, c, v)


def test_expiry_weekday_switches_on_the_given_date():
    sw = date(2025, 9, 1)
    assert er.expiry_weekday_for(date(2025, 8, 29), 1, sw) == 3
    assert er.expiry_weekday_for(date(2025, 9, 2), 1, sw) == 1
    assert er.expiry_weekday_for(date(2025, 8, 29), 1, None) == 1


def test_weekly_and_monthly_expiry_with_roll_back_for_a_holiday():
    sw = date(2025, 9, 1)
    days = {date(2025, 10, 1) + timedelta(days=k) for k in range(40)}
    mdays = {d.isoformat() for d in days if d.weekday() < 5}
    assert er.expiry_kind(date(2025, 10, 7), 1, sw, mdays) == "weekly"          # a Tuesday
    assert er.expiry_kind(date(2025, 10, 8), 1, sw, mdays) is None
    assert er.expiry_kind(date(2025, 10, 28), 1, sw, mdays) == "monthly"        # last Tuesday of October
    mdays.discard("2025-10-21")                                                 # holiday on a Tuesday: expiry moves to Monday
    assert er.expiry_kind(date(2025, 10, 20), 1, sw, mdays) == "weekly"
    assert er.expiry_kind(date(2025, 10, 21), 1, sw, mdays) is None


def test_pre_holiday_needs_a_missing_weekday_not_just_a_weekend():
    mdays = {"2025-10-16", "2025-10-17", "2025-10-20", "2025-10-23"}            # 21-22 Oct are weekdays with no session
    assert not er.pre_holiday(date(2025, 10, 17), date(2025, 10, 20), mdays)    # Friday -> Monday: weekend only
    assert er.pre_holiday(date(2025, 10, 20), date(2025, 10, 23), mdays)        # Mon -> Thu over two missing weekdays
    assert er.pre_holiday(date(2025, 10, 20), date(2025, 10, 23), None)         # guessed from the stock's own gap


def test_walk_stop_first_gap_and_target():
    flat = [(0.0, 100.0, 100.5, 99.5, 100.0, 1.0)] * 6
    assert er.walk(flat, 0, 100.0, 3.0, 6.0, 3) == (0.0, "time")
    both = [(0.0, 100.0, 107.0, 96.0, 100.0, 1.0)] * 3                           # one bar holds stop and target: stop first
    g, how = er.walk(both, 0, 100.0, 3.0, 6.0, 2)
    assert how == "stop" and abs(g + 3.0) < 1e-9
    gap = [(0.0, 95.0, 96.0, 94.0, 95.0, 1.0)] * 3                               # opens through the stop: exits at the open
    g, how = er.walk(gap, 0, 100.0, 3.0, 6.0, 2)
    assert how == "stop" and abs(g + 5.0) < 1e-9
    up = [(0.0, 100.0, 106.5, 99.5, 105.0, 1.0)] * 3
    g, how = er.walk(up, 0, 100.0, 3.0, 6.0, 2)
    assert how == "target" and abs(g - 6.0) < 1e-9


def test_stop_target_clamp_and_fallback():
    assert er.stop_target(None, 1.5, 2.0) == (3.0, 6.0)
    assert er.stop_target(0.5, 1.5, 2.0) == (2.0, 4.0)                           # floor 2 %
    assert er.stop_target(9.0, 1.5, 2.0) == (6.0, 12.0)                          # cap 6 %
    assert er.stop_target(2.0, 2.0, 1.5) == (4.0, 6.0)


def test_no_lookahead_signal_ignores_later_bars():
    rng = random.Random(3)
    d, bars, px = date(2023, 1, 2), [], 100.0
    while len(bars) < 330:
        if d.weekday() < 5:
            nx = px * (1 + rng.gauss(0, 0.012))
            bars.append(_bar(d, px, max(px, nx) * 1.004, min(px, nx) * 0.996, nx))
            px = nx
        d += timedelta(days=1)
    a, _ = er.rows_for(bars, "AAA", 3, 0.3, 0.1, 1, date(2025, 9, 1), None, 5)
    more = bars + [_bar(date(2030, 1, 1) + timedelta(days=k), 500.0, 510.0, 490.0, 500.0) for k in range(20)]
    b, _ = er.rows_for(more, "AAA", 3, 0.3, 0.1, 1, date(2025, 9, 1), None, 5)
    keys = ("px", "turn", "exp_any", "exp_month", "pre_hol", "friday")
    for x, y in zip(a, b):                                                       # the shared days keep the same flags (only forward returns may differ near the end)
        assert all(x[k] == y[k] for k in keys)


def _write_universe(folder: Path, n_sym=24, n_days=700, seed=11):
    rng = random.Random(seed)
    start = date(2022, 1, 3)
    hol = {start + timedelta(days=k) for k in range(0, 1000, 53)}
    days = [d for d in (start + timedelta(days=k) for k in range(1100)) if d.weekday() < 5 and d not in hol][:n_days]
    def dump(sym, price0, vol, drift):
        px, c = price0, []
        for d in days:
            nx = px * (1 + rng.gauss(drift, 0.015))
            c.append({"date": d.isoformat(), "open": px, "high": max(px, nx) * 1.005, "low": min(px, nx) * 0.995, "close": nx, "volume": vol * rng.uniform(0.7, 1.3)})
            px = nx
        (folder / f"{sym}.json").write_text(json.dumps({"candles": c}))
    dump("^NSEI", 18000.0, 1e8, 0.0003)
    for k in range(n_sym):
        dump(f"S{k:02d}", rng.choice([15.0, 40.0, 120.0, 800.0]), rng.choice([2e4, 2e5, 2e6]), 0.0002)


def test_end_to_end_report_files_and_shape():
    with tempfile.TemporaryDirectory() as t:
        folder = Path(t)
        _write_universe(folder)
        rc = er.main([str(folder), "--step", "5", "--boot", "100", "--min-n", "30"])
        assert rc == 0
        rep = json.loads((folder / "entry_out" / "entry_rules_report.json").read_text())
        assert set(rep["gates"]) == {g[0] for g in er.GATES}
        assert len(rep["grid"]["cells"]) == len(er.ATR_MULTS) * len(er.RRS)
        live = [c for c in rep["grid"]["cells"] if c["verdict"] == "live"]
        assert len(live) == 1 and abs(live[0]["diff_vs_live"]) < 1e-9
        assert rep["meta"]["calendar"] == "^NSEI benchmark dates"
        assert (folder / "entry_out" / "entry_rules_report.md").read_text().startswith("# Entry rules")
        assert rep["meta"]["symbols"] == 24            # the ^NSEI benchmark is calendar only, never traded


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
            print("ok", k)
    print(n, "tests passed")


# ── group 427: progress lines + benchmark-only fetch ──────────────────────────────────────────────────────────────────────────────────────
def _double(x):
    return x * 2


def test_run_jobs_keeps_order_and_reports_progress(capsys):
    from _progress import run_jobs
    assert run_jobs(_double, list(range(7)), 1, "t", every=3) == [0, 2, 4, 6, 8, 10, 12]
    err = capsys.readouterr().err
    assert "starting 7 symbols" in err and "3/7" in err and "7/7" in err


def test_run_jobs_in_a_pool_keeps_order(capsys):
    from _progress import run_jobs
    assert run_jobs(_double, list(range(9)), 2, "t", every=4, chunksize=2) == [x * 2 for x in range(9)]
    assert "9/9" in capsys.readouterr().err


def test_fetch_benchmark_only(tmp_path, monkeypatch, capsys):
    import fetch_history_for_backtest as f
    candles = [{"t": i} for i in range(300)]
    monkeypatch.setattr(f, "fetch_one", lambda *a, **k: candles)
    monkeypatch.setattr(f, "candles_of", lambda doc: doc["candles"] if isinstance(doc, dict) else doc)
    rc = f.main([str(tmp_path), "--base", "http://127.0.0.1:8001", "--benchmark-only"])
    assert rc == 0 and (tmp_path / "^NSEI.json").exists()
    assert f.main([str(tmp_path), "--base", "http://127.0.0.1:8001", "--benchmark-only"]) == 0     # second run: already there, no fetch
    assert "already there" in capsys.readouterr().out
    monkeypatch.setattr(f, "fetch_one", lambda *a, **k: [])
    assert f.main([str(tmp_path), "--base", "http://127.0.0.1:8001", "--benchmark-only", "--refresh"]) == 1
