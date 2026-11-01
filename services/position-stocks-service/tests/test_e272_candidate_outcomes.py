"""E272: candidate outcome labels. Plain runner: python3 tests/test_e272_candidate_outcomes.py (pytest collects it too)."""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders import candidate_outcomes as co

T0 = 1_793_000_000.0 - (1_793_000_000 % 60)          # a whole minute


def _bars(closes, hi_add=0.0, lo_sub=0.0):
    return [[T0 + 60 * i, c, c + hi_add, c - lo_sub, c, 100] for i, c in enumerate(closes)]


def test_forward_returns_from_the_last_bar_at_or_before_t0():
    bars = _bars([100.0] + [101.0] * 70)
    r = co.label(bars, T0)
    assert r["status"] == "ok" and r["r5"] == 1.0 and r["r60"] == 1.0


def test_short_flips_the_sign():
    bars = _bars([100.0] + [99.0] * 70)
    assert co.label(bars, T0, "SHORT")["r15"] == 1.0 and co.label(bars, T0, "LONG")["r15"] == -1.0


def test_mfe_and_mae_within_60_minutes():
    bars = _bars([100.0, 100.0, 103.0, 98.0, 100.0] + [100.0] * 70, 0.0, 0.0)
    r = co.label(bars, T0)
    assert r["mfe60"] == 3.0 and r["mae60"] == -2.0
    s = co.label(bars, T0, "SHORT")
    assert s["mfe60"] == 2.0 and s["mae60"] == -3.0


def test_horizon_beyond_the_data_is_none_not_guessed():
    r = co.label(_bars([100.0] * 12), T0)
    assert r["r5"] == 0.0 and r["r15"] is None and r["r60"] is None


def test_no_bars_or_last_bar_only():
    assert co.label(None, T0)["status"] == "no_bars"
    assert co.label(_bars([100.0]), T0)["status"] == "no_bars"
    assert co.label(_bars([100.0, 101.0]), T0 - 600)["status"] == "no_bars"      # before the first bar


def test_label_rows_dedupes_and_counts():
    bars = _bars([100.0] + [101.0] * 70)
    day = co.datetime.utcfromtimestamp(T0 + 19800).strftime("%Y-%m-%d")
    rows = [{"symbol": "AAA", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH:x", "created_ts": T0},
            {"symbol": "AAA", "decision": "SKIPPED", "reason": "RANGE_GATE:NEAR_DAY_HIGH:y", "created_ts": T0 + 30},      # same gate, same 5-min bucket
            {"symbol": "BBB", "decision": "ENTERED", "reason": None, "created_ts": T0}]
    res = co.label_rows(rows, lambda s, d: bars if (s == "AAA" and d == day) else None)
    assert res["counts"] == {"rows": 3, "labelled": 1, "no_bars": 1, "deduped": 1}
    assert [r["gate"] for r in res["rows"]] == ["RANGE_GATE:NEAR_DAY_HIGH", "ENTERED"]


def test_run_day_saves_a_file_and_reads_bar_files():
    with tempfile.TemporaryDirectory() as d:
        day = co.datetime.utcfromtimestamp(T0 + 19800).strftime("%Y-%m-%d")
        os.makedirs(os.path.join(d, day))
        json.dump({"bars": _bars([100.0] + [102.0] * 70)}, open(os.path.join(d, day, "AAA.json"), "w"))
        out = co.run_day([{"symbol": "AAA", "decision": "SKIPPED", "reason": "X_GATE", "created_ts": T0}], d, day)
        assert out["labelled"] == 1 and out["path"] and json.load(open(out["path"]))["rows"][0]["r30"] == 2.0
        assert co.save_day("/proc/nope/x", day, {}) is None            # unwritable -> None, never raises


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
