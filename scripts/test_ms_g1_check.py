"""Tests for scripts/ms_g1_check.py. Run: python3 scripts/test_ms_g1_check.py (plain asserts) or pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "shared" / "market_structure" / "tests"))

import ms_g1_check as g  # noqa: E402
from _bars import walk  # noqa: E402
from market_structure.params import MSParams  # noqa: E402


def _write(d: Path, name: str, bars):
    d.joinpath(f"{name}.json").write_text(json.dumps([{"date": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in bars]))


def _split(bars, k=300, f=0.5):
    return [b if j < k else (b[0], b[1] * f, b[2] * f, b[3] * f, b[4] * f, b[5]) for j, b in enumerate(bars)]


def test_clean_history_is_invariant_and_has_no_ca():
    bars = walk(450, seed=101)
    assert g.check_invariance(bars, MSParams()) == []
    assert g.check_corporate_action(bars, MSParams()) is None


def test_a_lookahead_engine_is_caught():
    bars = walk(300, seed=102)

    def leaky(b, p):                      # pretends the prefix run saw one extra swing that the full run does not have
        ev = g.events(b, p)
        if len(b) < len(bars):
            ev["swings"][(b[-1][0], "high", 1.0)] = b[-1][0]
        return ev
    assert g.check_invariance(bars, MSParams(), events_fn=leaky)


def test_split_is_reported_and_verified():
    ca = g.check_corporate_action(_split(walk(450, seed=103)), MSParams())
    assert ca and ca["ok"] and ca["jump_pct"] is not None and ca["jump_pct"] < -30


def test_verdicts_not_enough_data_then_pass():
    d = Path(tempfile.mkdtemp())
    for i in range(3):
        _write(d, f"S{i}", _split(walk(450, seed=110 + i)))
    r = g.run(d, min_symbols=20, min_ca=10)
    assert r["overall"] == g.NED and r["checks"]["truncation_invariance"]["verdict"] == g.NED     # too few symbols is never PASS
    for i in range(3, 12):
        _write(d, f"S{i}", _split(walk(450, seed=110 + i)))
    r = g.run(d, min_symbols=10, min_ca=10)
    assert r["overall"] == g.PASS and r["checks"]["corporate_actions"]["verified"] >= 10


def test_benchmark_is_not_replayed_and_main_writes_reports():
    d = Path(tempfile.mkdtemp())
    _write(d, "^NSEI", walk(450, seed=120))
    _write(d, "AAA", walk(450, seed=121))
    assert [s for s, _ in g.load(d)] == ["AAA"]
    assert g.main([str(d), "--min-symbols", "1", "--min-ca", "0", "--out", str(d / "o")]) == 0
    assert (d / "o" / "g1_report.json").exists() and (d / "o" / "g1_report.md").exists()
    assert g.main([str(d / "nope")]) == 2


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")


def test_a_split_just_before_a_cut_is_not_a_violation():
    """Real VM run (group 405): a split 30-50 bars before a cut leaves the prefix with < min_bars in its segment, the engine returns nothing for it
    (quality `insufficient`) while the full run has more bars. That is not look-ahead: only cuts where the prefix makes a claim are compared."""
    from _bars import scale
    p = MSParams()
    for jump_at in (30, 45, 100, 300):
        b = walk(500, seed=3)
        assert g.check_invariance(b[:jump_at] + scale(b[jump_at:], 0.5), p, 6) == [], jump_at


def test_latency_is_cpu_time_so_a_cpu_limit_pause_is_not_counted():
    """E229: a container CPU cap pauses the process mid-call; wall time then reads ~50 ms too long, CPU time does not."""
    import time as _t
    real_analyze = g.analyze

    def paused(bars, params=None, **k):
        out = real_analyze(bars, params=params, **k)
        _t.sleep(0.05)          # what a cpus 0.50 throttle looks like: wall time passes, no CPU is used
        return out

    g.analyze = paused
    try:
        wall = []
        cpu = g.check_latency(walk(300, seed=1), MSParams(), reps=2, wall_out=wall)
    finally:
        g.analyze = real_analyze
    assert len(cpu) == 2 and len(wall) == 2
    assert all(w >= 50 for w in wall)          # the pause is visible in wall time
    assert all(c < 40 for c in cpu)            # and not in CPU time
