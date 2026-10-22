"""group491: structure_backtest.py per-symbol checkpoint / resume. Offline, stdlib only. Plain asserts or pytest."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import structure_backtest as sb  # noqa: E402

CALLS: list = []


def _candles(n=150, base=100.0):
    return [{"date": f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": base + i * 0.1, "high": base + i * 0.1 + 1, "low": base + i * 0.1 - 1,
             "close": base + i * 0.1 + 0.2, "volume": 1000.0} for i in range(n)]


def _fake_net_samples(bars, step=3, cost_pct=0.30, slip_pct=0.10):
    CALLS.append((len(bars), step, cost_pct, slip_pct))
    return [{"i": 1, "ts": 1, "price": 100.0, "summary": {}, "outcome": {"ret_10d": 1.0 + len(CALLS)}}]


def _hist(names=("AAA", "BBB", "CCC", "DDD")):
    d = tempfile.mkdtemp()
    for n in names:
        Path(d, f"{n}.json").write_text(json.dumps({"candles": _candles()}))
    Path(d, "^NSEI.json").write_text(json.dumps({"candles": _candles()}))
    return d


def _run(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sb.main(args)
    return code, out.getvalue(), err.getvalue()


def _patched():
    orig = sb.net_samples
    sb.net_samples = _fake_net_samples
    return orig


def test_fresh_run_saves_each_symbol_and_resume_computes_only_the_missing():
    d, ck = _hist(), os.path.join(tempfile.mkdtemp(), "ck")
    orig = _patched()
    try:
        CALLS.clear()
        code, out, _ = _run([d, "--checkpoint", ck])
        assert code == 0 and len(CALLS) == 4 and len(os.listdir(ck)) == 4          # benchmark never traded, never saved
        first = json.loads(out)
        assert first["symbols"] == 4 and first["samples"] == 4
        for f in sorted(os.listdir(ck))[2:]:
            os.remove(os.path.join(ck, f))
        CALLS.clear()
        code, out2, err = _run([d, "--checkpoint", ck, "--resume"])
        assert code == 0 and len(CALLS) == 2 and "resuming: 2 already done, 2 to go" in err
        assert json.loads(out2)["symbols"] == 4 and json.loads(out2)["samples"] == 4
    finally:
        sb.net_samples = orig


def test_without_resume_the_checkpoint_is_emptied_and_everything_recomputed():
    d, ck = _hist(), os.path.join(tempfile.mkdtemp(), "ck")
    orig = _patched()
    try:
        _run([d, "--checkpoint", ck])
        CALLS.clear()
        _run([d, "--checkpoint", ck])
        assert len(CALLS) == 4
    finally:
        sb.net_samples = orig


def test_resume_reuses_saved_symbols_when_only_horizon_or_thresholds_change_but_not_when_step_or_costs_change():
    d, ck = _hist(("AAA", "BBB")), os.path.join(tempfile.mkdtemp(), "ck")
    orig, orig_build = _patched(), sb.build_samples
    try:
        _run([d, "--checkpoint", ck])
        CALLS.clear()
        _run([d, "--checkpoint", ck, "--resume", "--horizon", "5", "--min-n", "5", "--thresholds", '{"chase_atr":1.5}'])
        assert CALLS == []                                                       # samples reused: only the cheap walk-forward re-ran
        for extra in (["--step", "5"], ["--cost-pct", "0.5"], ["--slip-pct", "0.2"], ["--entry", "close"]):
            CALLS.clear()
            sb.build_samples = lambda bars, step=3: _fake_net_samples(bars, step)
            _run([d, "--checkpoint", ck, "--resume", *extra])
            assert len(CALLS) == 2, extra
    finally:
        sb.net_samples, sb.build_samples = orig, orig_build


def test_a_changed_history_file_is_recomputed_on_resume():
    d, ck = _hist(("AAA", "BBB")), os.path.join(tempfile.mkdtemp(), "ck")
    orig = _patched()
    try:
        _run([d, "--checkpoint", ck])
        Path(d, "AAA.json").write_text(json.dumps({"candles": _candles(180)}))
        CALLS.clear()
        _run([d, "--checkpoint", ck, "--resume"])
        assert len(CALLS) == 1 and CALLS[0][0] == 180
    finally:
        sb.net_samples = orig


def test_short_and_unreadable_files_are_skipped_not_counted_and_not_fatal():
    d = _hist(("AAA",))
    Path(d, "SHORT.json").write_text(json.dumps({"candles": _candles(50)}))
    Path(d, "BAD.json").write_text("{not json")
    orig = _patched()
    try:
        code, out, err = _run([d])
        assert code == 0 and json.loads(out)["symbols"] == 1 and "skip BAD:" in err
    finally:
        sb.net_samples = orig


def test_no_usable_symbol_exits_1():
    d = tempfile.mkdtemp()
    Path(d, "SHORT.json").write_text(json.dumps({"candles": _candles(50)}))
    assert _run([d])[0] == 1


def test_parallel_workers_give_the_same_samples_as_one_process():
    d = _hist(("AAA", "BBB", "CCC"))
    a = [sb._job((str(Path(d, f"{n}.json")), 3, "close", 0.3, 0.1)) for n in ("AAA", "BBB", "CCC")]
    assert [r["sym"] for r in a] == ["AAA", "BBB", "CCC"] and all(r["err"] == "" for r in a)
    from _progress import run_jobs
    jobs = [(str(Path(d, f"{n}.json")), 3, "close", 0.3, 0.1) for n in ("AAA", "BBB", "CCC")]
    with contextlib.redirect_stderr(io.StringIO()):
        b = run_jobs(sb._job, jobs, 2, "structure")
    assert [r["sym"] for r in b] == ["AAA", "BBB", "CCC"] and [len(r["samples"]) for r in b] == [len(r["samples"]) for r in a]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
