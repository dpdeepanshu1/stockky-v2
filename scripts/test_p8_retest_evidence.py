"""group441: scripts/p8_retest_evidence.py. Plain asserts (python3 scripts/test_p8_retest_evidence.py) or pytest."""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "shared"))

import p8_retest_evidence as p8  # noqa: E402

DAY = 86400.0
T0 = 1_700_000_000.0 - (1_700_000_000.0 % DAY)


def _s(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_target": 108.0, "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


OLD_RETEST = dict(ms_bos_bars=14, ms_choch_dir="bullish", ms_choch_bars=2, ms_fvg_side="bullish", ms_fvg_top=101.0, ms_fvg_bottom=99.0, ms_fvg_status="fresh")


def test_clean_rescued_blocked_other():
    assert p8.classify(_s()) == "clean"
    assert p8.classify(_s(**OLD_RETEST)) == "rescued"
    assert p8.classify(_s(ms_bos_bars=14)) == "blocked"
    assert p8.classify(_s(**{**OLD_RETEST, "ms_fvg_status": "filled"})) == "blocked"
    assert p8.classify(_s(**{**OLD_RETEST, "ms_choch_bars": 9})) == "blocked"
    assert p8.classify(_s(**{**OLD_RETEST, "ms_groups": 1})) == "other"
    assert p8.classify(_s(**{**OLD_RETEST, "ms_trend": "bearish"})) == "blocked"


def test_a_rescued_day_is_not_rescued_when_another_rule_still_trips():
    assert p8.classify(_s(**OLD_RETEST, ms_target=101.0)) == "other"


def test_zones_matched():
    s = _s(**OLD_RETEST, ms_in_golden=True)
    assert p8.zones_matched(s) == ["fvg", "golden"]
    assert p8.zones_matched(_s(ms_price=None)) == []


def _rows(g, n, val, sym="A", month="2026-01", ts0=0):
    return [{"sym": f"{sym}{k % 25}", "month": month, "g": g, "ts": ts0 + k, "zones": [], "stop_first": False, "bull": True, "r10": val, "r5": val, "r20": val, "r1": val}
            for k in range(n)]


def test_verdicts():
    v = p8.verdict
    assert v(0, 0, 0, None, None, None, None, [None, None], 100) == "no_data"
    assert v(10, 500, 900, 1.0, [0.1, 1], [0, 1], 1.0, [1, 1], 100) == "insufficient_sample"
    assert v(300, 300, 900, 1.0, [0.2, 1.4], [-0.5, 0.5], 0.8, [0.5, 1.0], 100) == "retest_ok"
    assert v(300, 300, 900, 1.0, [-0.2, 1.4], [-0.5, 0.5], 0.8, [0.5, 1.0], 100) == "no_edge"
    assert v(300, 300, 900, 1.0, [0.2, 1.4], [-0.5, 0.5], 0.8, [0.5, -0.1], 100) == "no_edge"
    assert v(300, 300, 900, 0.3, [-1.4, -0.2], [-0.5, 0.5], -0.8, [-0.5, -1.0], 100) == "retest_hurts"


def test_bootstrap_clear_difference_and_empty_side():
    a = _rows("rescued", 200, 3.0)
    b = _rows("blocked", 200, 0.0)
    out = p8.boot_diff(a, b, 10, 200)
    assert out["ci90"] is not None and out["ci90"][0] > 2.0 and out["p_positive"] == 1.0
    assert p8.boot_diff(a, [], 10, 200)["ci90"] is None
    assert p8.boot_diff(a[:5], b[:5], 10, 200)["ci90"] is None


def test_report_end_to_end_on_hand_rows():
    rows = _rows("rescued", 150, 2.0) + _rows("blocked", 150, 0.0) + _rows("clean", 100, 1.0) + _rows("other", 40, -1.0)
    meta = {"symbols": 3, "skipped": 0, "benchmark": "none", "step": 3, "cost_pct": 0.3, "slip_pct": 0.1, "thresholds": {}, "sample": {}}
    rep = p8.build_report(rows, 10, 100, 200, meta)
    assert rep["counts"] == {"clean": 100, "rescued": 150, "blocked": 150, "other": 40}
    assert rep["rescued_minus_blocked_pp"] == 2.0 and rep["rescued_minus_clean_pp"] == 1.0
    assert rep["added_share_of_buy_now_pct"] == 60.0
    assert rep["verdict"] in ("retest_ok", "no_edge")
    assert "Verdict" in p8.to_markdown(rep)


def _walk_file(path: Path, seed: int, n: int = 520):
    rnd = random.Random(seed)
    px, out = 100.0, []
    for k in range(n):
        o = px
        c = max(5.0, o * (1 + rnd.gauss(0.0004, 0.014)))
        out.append({"date": T0 + k * DAY, "open": o, "high": max(o, c) * 1.006, "low": min(o, c) * 0.994, "close": c, "volume": 100000.0})
        px = c
    path.write_text(json.dumps({"candles": out}))


def test_cli_runs_on_synthetic_histories_and_writes_the_report():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for k in range(3):
            _walk_file(d / f"SYM{k}.json", seed=k + 1)
        rc = p8.main([td, "--step", "5", "--min-n", "5", "--boot", "50"])
        assert rc in (0, 1)
        rep = json.loads((d / "p8_out" / "p8_retest.json").read_text())
        assert rep["verdict"] in ("no_data", "insufficient_sample", "retest_ok", "retest_hurts", "no_edge")
        assert sum(rep["counts"].values()) > 0 and (d / "p8_out" / "p8_retest.md").exists()


def test_empty_folder_is_no_data_and_exit_1():
    with tempfile.TemporaryDirectory() as td:
        assert p8.main([td, "--boot", "50"]) == 1
        assert json.loads((Path(td) / "p8_out" / "p8_retest.json").read_text())["verdict"] == "no_data"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
