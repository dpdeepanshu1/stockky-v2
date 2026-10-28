"""E240: a second heavy run starts only when the container limit still has room for what the running heavy runs WILL use.
28 Oct: p8_retest and ml_v2 (3 pool workers each) started in the same second, the 6000 MB container was OOM-killed and 8 runs failed
('the runner restarted while this run was active'). Offline, stdlib only."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import runner as R  # noqa: E402
from test_runner_engine import _runner  # noqa: E402


def _mk(jobs=None, **kw):
    r, d, sc = _runner({}, workers=4, max_heavy=2, min_free_mb=1500, **kw)
    r.shutdown()                       # the pool threads must not pick the hand-made runs below
    for t in r.threads:
        t.join(2)
    return r


def _run(r, rid, jobs=3, heavy=True):
    x = R.Run(rid, "job", rid, rid, {"jobs": jobs} if jobs is not None else {}, "t", "manual")
    x.heavy = heavy
    return x


def test_footprint_is_parent_plus_one_process_per_pool_worker():
    r = _mk()
    r.heavy_proc_mb, r.default_pool_jobs = 800.0, 3
    assert r._heavy_footprint_mb(_run(r, "a", jobs=3)) == 3200.0
    assert r._heavy_footprint_mb(_run(r, "b", jobs=1)) == 1600.0
    assert r._heavy_footprint_mb(_run(r, "c", jobs=None)) == 3200.0      # no 'jobs' option -> the default pool size


def test_second_heavy_waits_when_two_replays_would_not_fit_in_6000_mb():
    r = _mk()
    r.heavy_proc_mb = 800.0
    r.running = [_run(r, "p8", jobs=3)]
    assert r._heavy_fits(_run(r, "ml", jobs=3), 6000.0) is False          # 6000 - 3200 - 3200 < 1500: the 28 Oct case
    assert r._heavy_fits(_run(r, "ml", jobs=1), 6000.0) is False         # 6000 - 3200 - 1600 = 1200 < 1500
    assert r._heavy_fits(_run(r, "ml", jobs=1), 6400.0) is True           # 6400 - 3200 - 1600 = 1600 >= 1500


def test_fits_arithmetic_and_unknown_limit():
    r = _mk()
    r.heavy_proc_mb = 500.0
    r.running = [_run(r, "a", jobs=2)]                                    # 1500 committed
    assert r._heavy_fits(_run(r, "b", jobs=2), 6000.0) is True            # 6000-1500-1500 = 3000 >= 1500
    assert r._heavy_fits(_run(r, "b", jobs=2), 4400.0) is False           # 1400 < 1500
    assert r._heavy_fits(_run(r, "b", jobs=2), None) is True              # no limit known -> allowed (the cap of RUNNER_MAX_HEAVY still applies)
    r.running = [_run(r, "light", jobs=3, heavy=False)]
    assert r._heavy_fits(_run(r, "b", jobs=2), 3200.0) is True            # light runs do not count


def test_pick_holds_back_the_second_heavy_but_not_a_light_run(monkeypatch=None):
    r = _mk()
    r.heavy_proc_mb, r.default_pool_jobs = 800.0, 3
    orig_lim, orig_av = R._mem_limit_mb, R._mem_available_mb
    R._mem_limit_mb, R._mem_available_mb = (lambda: 6000.0), (lambda: 8000.0)
    try:
        first, second, light = _run(r, "p8", 3), _run(r, "ml", 3), _run(r, "light", None, heavy=False)
        r.running = [first]
        r.queue.extend([second, light])
        assert r._pick() is light                                         # the heavy one waits, the light one is not held behind it
        r.queue.remove(light)
        assert r._pick() is None
        R._mem_limit_mb = lambda: 12000.0                                 # a bigger container: both fit
        assert r._pick() is second
    finally:
        R._mem_limit_mb, R._mem_available_mb = orig_lim, orig_av


def test_wait_reason_names_memory():
    r = _mk()
    r.heavy_proc_mb = 800.0
    orig = R._mem_limit_mb
    R._mem_limit_mb = lambda: 6000.0
    try:
        r.running = [_run(r, "p8", jobs=3)]
        w = _run(r, "ml", jobs=3)
        r.queue.append(w)
        r.workers = 4
        assert "waiting for memory" in r._why_waiting(w)
    finally:
        R._mem_limit_mb = orig


def test_limit_reader_and_wiring():
    assert R._mem_limit_mb() is None or R._mem_limit_mb() > 0
    src = (HERE.parent / "main.py").read_text()
    assert "heavy_proc_mb=config.HEAVY_PROC_MB" in src and "default_pool_jobs=config.DEFAULT_POOL_JOBS" in src
    assert "RUNNER_HEAVY_PROC_MB" in (HERE.parent / "config.py").read_text()
    assert "RUNNER_HEAVY_PROC_MB" in (HERE.parent.parent.parent / "docker-compose.yml").read_text()
